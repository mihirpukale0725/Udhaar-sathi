"""Payment flow: reminder -> payment link -> fake UPI page -> signed webhook.

MOCKED: there is no real UPI or Razorpay here. The pieces map to the real
world like this:

    send_reminder()       shop sends a WhatsApp message with a payment link
    GET /pay/{token}      the gateway's hosted payment page
    gateway_checkout()    the gateway's server: takes the payment, signs a webhook
    handle_webhook()      OUR server: verifies the signature, updates the khata

In production the gateway calls our webhook server-to-server. In this demo
the browser delivers the signed webhook so you can watch it in DevTools, but
the signature is still created on the server: WEBHOOK_SECRET never reaches
the browser.

Two guarantees the webhook gives:
- Authentic: requests without a valid HMAC-SHA256 signature get 401.
- Idempotent: gateways retry webhooks, so the same payment_id arriving twice
  is recorded once (checked in code, and backed by a UNIQUE column).
"""

import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import sqlite3
from functools import cache

from pydantic import ValidationError

from app import khata
from app.models import WebhookPayload

log = logging.getLogger(__name__)

FIRM_TONE_AFTER_DAYS = 15


# ---------------------------------------------------------------- errors ---
# Subclasses of KhataError, so main.py's single error handler covers them.

class InvalidSignature(khata.KhataError):
    code = "invalid_signature"


class LinkNotFound(khata.KhataError):
    code = "link_not_found"


class LinkUnavailable(khata.KhataError):
    code = "link_unavailable"   # already paid, or out of date


class BadPayload(khata.KhataError):
    code = "bad_payload"


# --------------------------------------------------------------- config ---

def shop_name() -> str:
    return os.getenv("SHOP_NAME") or "Saathi Kirana Store"


def base_url() -> str:
    """Where the app is reachable; used to build links inside WhatsApp messages.

    RENDER_EXTERNAL_URL is set automatically by Render, so deployed links work
    without extra configuration.
    """
    url = os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "http://127.0.0.1:8000"
    return url.rstrip("/")


@cache
def _random_dev_secret() -> bytes:
    # No hardcoded fallback: a default like "secret" would be guessable by anyone
    # who reads this code on GitHub. A random per-process secret keeps the demo
    # working (signer and verifier run in the same process) without that risk.
    log.warning("WEBHOOK_SECRET is not set; using a random secret for this run.")
    return secrets.token_hex(32).encode()


def _secret() -> bytes:
    secret = os.getenv("WEBHOOK_SECRET")
    return secret.encode() if secret else _random_dev_secret()


# ------------------------------------------------------------- signature ---

def sign(body: bytes) -> str:
    """Hex HMAC-SHA256 of the raw request body."""
    return hmac.new(_secret(), body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, signature: str | None) -> None:
    """Raise InvalidSignature unless `signature` matches `body`.

    We sign the RAW bytes, never re-serialized JSON: parsing and dumping again
    could change spacing or key order and break a valid signature.
    compare_digest takes the same time whether the first or the last character
    differs, so an attacker can't guess the signature one character at a time
    by measuring response times (a "timing attack").
    """
    if not signature or not hmac.compare_digest(sign(body), signature):
        raise InvalidSignature("Webhook signature is missing or invalid.")


# ------------------------------------------------------------- reminders ---

def _get_or_create_link(conn: sqlite3.Connection, customer_id: int, amount: int) -> str:
    """Reuse an unpaid link for the same amount, so repeated reminders don't
    leave many valid links lying around. Otherwise create a new one."""
    row = conn.execute(
        "SELECT token FROM payment_links WHERE customer_id = ? AND amount = ? AND status = 'pending' "
        "ORDER BY created_at DESC LIMIT 1",
        (customer_id, amount),
    ).fetchone()
    if row:
        return row["token"]

    token = secrets.token_urlsafe(16)  # ~128 bits of randomness: impossible to guess
    conn.execute(
        "INSERT INTO payment_links (token, customer_id, amount) VALUES (?, ?, ?)",
        (token, customer_id, amount),
    )
    return token


def reminder_text(name: str, balance: int, days: int | None, link: str) -> str:
    """Polite by default; firmer (but still respectful) after FIRM_TONE_AFTER_DAYS."""
    if days is not None and days > FIRM_TONE_AFTER_DAYS:
        return (f"{name} ji, {shop_name()} se: aapka ₹{balance} ka udhaar {days} din se baaki hai. "
                f"Kripya aaj hi is link se payment karein: {link}")
    return (f"Namaste {name} ji! {shop_name()} se chhota sa reminder: aapka ₹{balance} baaki hai. "
            f"Jab suvidha ho, is link se pay kar dijiye: {link} Dhanyavaad!")


def _queue_reminder(conn: sqlite3.Connection, customer: dict) -> dict:
    """Create/reuse a link and 'send' the WhatsApp message (MOCK: a messages row)."""
    token = _get_or_create_link(conn, customer["id"], customer["balance"])
    link = f"{base_url()}/pay/{token}"
    body = reminder_text(customer["name"], customer["balance"], customer["days_pending"], link)
    conn.execute(
        "INSERT INTO messages (customer_id, direction, kind, body) VALUES (?, 'out', 'reminder', ?)",
        (customer["id"], body),
    )
    firm = (customer["days_pending"] or 0) > FIRM_TONE_AFTER_DAYS
    return {"customer": customer, "body": body, "link": link, "token": token, "firm": firm}


def send_reminder(conn: sqlite3.Connection, customer_name: str) -> dict:
    """Send one customer (found by fuzzy name, e.g. from voice) a reminder."""
    return send_reminder_to(conn, khata.find_customer(conn, customer_name)["id"])


def send_reminder_to(conn: sqlite3.Connection, customer_id: int) -> dict:
    """Send a reminder with a payment link for the customer's full balance."""
    with conn:
        customer = khata.customer_summary(conn, customer_id)
        if customer["balance"] <= 0:
            raise khata.InvalidAmount(f"{customer['name']} has no pending udhaar.",
                                      balance=0, customer=customer["name"])
        return _queue_reminder(conn, customer)


def remind_all(conn: sqlite3.Connection) -> list[dict]:
    """Remind everyone who owes money. All messages are saved together, or none."""
    pending = khata.list_pending(conn)
    with conn:
        return [_queue_reminder(conn, c) for c in pending]


# ---------------------------------------------------------- payment links ---

def get_link(conn: sqlite3.Connection, token: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT pl.*, c.name AS customer_name FROM payment_links pl "
        "JOIN customers c ON c.id = pl.customer_id WHERE pl.token = ?",
        (token,),
    ).fetchone()
    if row is None:
        raise LinkNotFound("This payment link does not exist.")
    return row


def link_state(conn: sqlite3.Connection, link: sqlite3.Row) -> str:
    """'paid', 'stale' (customer now owes less than the link asks for, e.g. they
    paid some cash after the reminder), or 'payable'."""
    if link["status"] == "paid":
        return "paid"
    if link["amount"] > khata.get_balance(conn, link["customer_id"]):
        return "stale"
    return "payable"


def gateway_checkout(conn: sqlite3.Connection, token: str, method: str) -> dict:
    """FAKE GATEWAY. Pretend the customer paid, and return a signed webhook.

    In real life this is Razorpay's server, after real money moved.
    """
    link = get_link(conn, token)
    state = link_state(conn, link)
    if state == "paid":
        raise LinkUnavailable("This link has already been paid.")
    if state == "stale":
        raise LinkUnavailable("This link is out of date. Please ask the shop for a new one.")

    payload = json.dumps({
        "event": "payment.captured",
        "payment_id": "pay_" + secrets.token_hex(8),
        "token": token,
        "amount": link["amount"],
        "method": method,
    })
    return {"payload": payload, "signature": sign(payload.encode())}


# --------------------------------------------------------------- webhook ---

def thank_you_text(name: str, amount: int, balance: int) -> str:
    status = "Aapka khata clear hai." if balance <= 0 else f"Baaki: ₹{balance}."
    return f"Dhanyavaad {name} ji! ₹{amount} ka payment mil gaya. {status} - {shop_name()}"


def handle_webhook(conn: sqlite3.Connection, body: bytes, signature: str | None) -> dict:
    """Verify, then apply a 'payment.captured' webhook exactly once."""
    # 1. Authenticate BEFORE trusting anything in the body.
    verify_signature(body, signature)

    # 2. Validate the shape.
    try:
        payload = WebhookPayload.model_validate_json(body)
    except ValidationError:
        raise BadPayload("Webhook body is not a valid payment event.")

    with conn:
        # 3. Lock the DB so two copies of the same webhook can't both pass the
        #    "already processed?" check at the same time.
        conn.execute("BEGIN IMMEDIATE")

        # 4. Idempotency: seen this payment before? Reply 200 so the gateway
        #    stops retrying, but change nothing.
        if conn.execute("SELECT 1 FROM payment_links WHERE payment_id = ?", (payload.payment_id,)).fetchone():
            return {"status": "already_processed", "payment_id": payload.payment_id}

        link = get_link(conn, payload.token)
        if link["status"] == "paid":
            # A *different* payment for a link that's already paid: the customer
            # paid twice. A real shop would refund it; we refuse to record it.
            raise LinkUnavailable("This link was already paid by another payment.")
        if payload.amount != link["amount"]:
            raise BadPayload(f"Amount {payload.amount} does not match link amount {link['amount']}.")

        # 5. Apply: ledger entry, close the link, thank the customer.
        khata.record_received_payment(
            conn, link["customer_id"], payload.amount, f"UPI ({payload.method}) {payload.payment_id}"
        )
        conn.execute(
            "UPDATE payment_links SET status = 'paid', payment_id = ? WHERE token = ?",
            (payload.payment_id, payload.token),
        )
        balance = khata.get_balance(conn, link["customer_id"])
        conn.execute(
            "INSERT INTO messages (customer_id, direction, kind, body) VALUES (?, 'out', 'thank_you', ?)",
            (link["customer_id"], thank_you_text(link["customer_name"], payload.amount, balance)),
        )

    return {"status": "processed", "payment_id": payload.payment_id, "balance": balance}


# ------------------------------------------------------------- pay page ---

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pay {{SHOP}}</title>
<style>
  body { margin: 0; font-family: system-ui, sans-serif; background: #f2f4f7; color: #1d2433; }
  .card { max-width: 380px; margin: 40px auto; background: #fff; border-radius: 16px;
          padding: 28px 24px; box-shadow: 0 4px 20px rgba(0,0,0,.08); text-align: center; }
  .demo { background: #fff4d6; color: #7a5a00; font-size: 12px; padding: 6px; border-radius: 8px; }
  .shop { font-size: 20px; font-weight: 700; margin: 18px 0 4px; }
  .to { color: #667085; font-size: 14px; }
  .amount { font-size: 44px; font-weight: 800; margin: 18px 0; }
  button { display: block; width: 100%; padding: 14px; margin: 10px 0; border: 0; border-radius: 10px;
           font-size: 16px; font-weight: 600; color: #fff; cursor: pointer; }
  button:disabled { opacity: .5; cursor: wait; }
  .paytm { background: #00baf2; } .gpay { background: #1a73e8; } .phonepe { background: #5f259f; }
  #status { min-height: 24px; margin-top: 12px; font-weight: 600; }
  .big { font-size: 56px; margin: 10px 0; }
</style>
</head>
<body>
<div class="card">
  <div class="demo">DEMO PAGE: no real money moves</div>
  <div class="shop">{{SHOP}}</div>
  <div class="to">{{SUBTITLE}}</div>
  {{CONTENT}}
</div>
</body>
</html>"""

PAY_BUTTONS = """
  <div class="amount">₹{{AMOUNT}}</div>
  <button class="paytm" onclick="pay('paytm')">Pay with Paytm</button>
  <button class="gpay" onclick="pay('gpay')">Pay with Google Pay</button>
  <button class="phonepe" onclick="pay('phonepe')">Pay with PhonePe</button>
  <div id="status"></div>
<script>
const TOKEN = {{TOKEN_JSON}};

async function pay(method) {
  const status = document.getElementById("status");
  document.querySelectorAll("button").forEach(b => b.disabled = true);
  status.textContent = "Processing...";
  try {
    // Step 1: the fake gateway (our server, playing Razorpay) "takes" the
    // payment and returns a signed webhook. The secret stays on the server.
    const g = await fetch(`/gateway/checkout/${TOKEN}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ method }),
    });
    const signed = await g.json();
    if (!g.ok) throw new Error(signed.detail);

    // Step 2: deliver the webhook with its HMAC-SHA256 signature header.
    // The body must be sent exactly as signed, byte for byte.
    const w = await fetch("/webhook/payment", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Signature": signed.signature },
      body: signed.payload,
    });
    const result = await w.json();
    if (!w.ok) throw new Error(result.detail);

    document.querySelectorAll("button").forEach(b => b.remove());
    status.innerHTML = '<div class="big">✅</div>Payment successful!';
  } catch (err) {
    status.textContent = "❌ " + err.message;
    document.querySelectorAll("button").forEach(b => b.disabled = false);
  }
}
</script>"""

PAID = '<div class="big">✅</div><p>₹{{AMOUNT}} already paid. Dhanyavaad!</p>'
STALE = '<div class="big">⌛</div><p>This link is out of date. Please ask the shop for a new one.</p>'
NOT_FOUND = '<div class="big">🔍</div><p>Payment link not found.</p>'


def _render(template: str, **values: str) -> str:
    # Plain {{KEY}} replacement. Callers must escape any user data first.
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    return template


def render_pay_page(conn: sqlite3.Connection, token: str) -> tuple[int, str]:
    """Return (HTTP status, HTML) for the fake UPI page."""
    shop = html.escape(shop_name())
    try:
        link = get_link(conn, token)
    except LinkNotFound:
        return 404, _render(PAGE, SHOP=shop, SUBTITLE="", CONTENT=NOT_FOUND)

    # html.escape: a customer named "<script>..." must show as text, not run.
    subtitle = f"Payment from {html.escape(link['customer_name'])}"
    amount = str(link["amount"])
    content = {
        "payable": _render(PAY_BUTTONS, AMOUNT=amount, TOKEN_JSON=json.dumps(token)),
        "paid": _render(PAID, AMOUNT=amount),
        "stale": STALE,
    }[link_state(conn, link)]
    return 200, _render(PAGE, SHOP=shop, SUBTITLE=subtitle, CONTENT=content)
