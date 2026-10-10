"""Payment flow: reminder links, fake UPI page, signed + idempotent webhook."""

import json
import threading
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app import khata, payments
from app.db import get_connection
from app.main import app


@pytest.fixture
def client(shop_db):
    with TestClient(app) as c:
        yield c


def owe(conn, customer_id, amount, days_ago=0):
    conn.execute(
        "INSERT INTO ledger (customer_id, type, amount, created_at) "
        "VALUES (?, 'udhaar', ?, datetime('now', 'localtime', ?))",
        (customer_id, amount, f"-{days_ago} days"),
    )
    conn.commit()


def remind_and_pay(client, conn, name="Ramesh", method="gpay"):
    """Send a reminder, then do exactly what the pay page's JavaScript does."""
    token = payments.send_reminder(conn, name)["token"]
    signed = client.post(f"/gateway/checkout/{token}", json={"method": method}).json()
    response = client.post("/webhook/payment", content=signed["payload"],
                           headers={"X-Signature": signed["signature"]})
    return token, signed, response


def link_status(conn, token):
    return conn.execute("SELECT status FROM payment_links WHERE token = ?", (token,)).fetchone()["status"]


def count(conn, table, where="1=1"):
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


# ------------------------------------------------------------- reminders ---

def test_reminder_creates_link_and_polite_message(shop_db):
    owe(shop_db, 1, 500, days_ago=3)
    r = payments.send_reminder(shop_db, "Ramesh")

    assert not r["firm"]
    assert r["body"].startswith("Namaste Ramesh Patil ji!")
    assert f"/pay/{r['token']}" in r["body"]
    link = payments.get_link(shop_db, r["token"])
    assert (link["amount"], link["status"]) == (500, "pending")
    assert count(shop_db, "messages") == 1


def test_reminder_is_firm_after_15_days(shop_db):
    owe(shop_db, 1, 500, days_ago=20)
    r = payments.send_reminder(shop_db, "Ramesh")
    assert r["firm"]
    assert "20 din se baaki" in r["body"] and "aaj hi" in r["body"]


def test_reminder_at_exactly_15_days_is_still_polite(shop_db):
    owe(shop_db, 1, 500, days_ago=15)
    assert not payments.send_reminder(shop_db, "Ramesh")["firm"]


def test_repeat_reminder_reuses_link_until_amount_changes(shop_db):
    owe(shop_db, 1, 500)
    first = payments.send_reminder(shop_db, "Ramesh")["token"]
    assert payments.send_reminder(shop_db, "Ramesh")["token"] == first

    owe(shop_db, 1, 100)  # balance is now 600 -> new link
    assert payments.send_reminder(shop_db, "Ramesh")["token"] != first


def test_no_reminder_or_link_when_nothing_owed(shop_db):
    with pytest.raises(khata.InvalidAmount):
        payments.send_reminder(shop_db, "Sunita")
    assert count(shop_db, "payment_links") == 0


def test_remind_all_creates_one_link_each(shop_db):
    owe(shop_db, 1, 500)
    owe(shop_db, 3, 200)
    sent = payments.remind_all(shop_db)
    assert {r["customer"]["name"] for r in sent} == {"Ramesh Patil", "Suresh Gupta"}
    assert count(shop_db, "payment_links") == 2


# ------------------------------------------------------------- signature ---

def test_signature_roundtrip():
    body = b'{"a":1}'
    payments.verify_signature(body, payments.sign(body))  # no exception


@pytest.mark.parametrize("signature", [None, "", "abc", "0" * 64])
def test_bad_signatures_are_rejected(signature):
    with pytest.raises(payments.InvalidSignature):
        payments.verify_signature(b'{"a":1}', signature)


def test_signature_depends_on_secret(monkeypatch):
    body = b'{"a":1}'
    sig = payments.sign(body)
    monkeypatch.setenv("WEBHOOK_SECRET", "a-different-secret")
    with pytest.raises(payments.InvalidSignature):
        payments.verify_signature(body, sig)


# --------------------------------------------------------- full pay flow ---

def test_full_payment_flow_closes_the_khata(client, shop_db):
    owe(shop_db, 1, 500)
    token, signed, r = remind_and_pay(client, shop_db)

    assert r.status_code == 200
    assert r.json()["status"] == "processed"
    assert r.json()["balance"] == 0
    assert khata.get_balance(shop_db, 1) == 0
    assert link_status(shop_db, token) == "paid"

    # Thank-you message is the newest message.
    newest = client.get("/messages?customer_id=1").json()[0]
    assert newest["body"].startswith("Dhanyavaad Ramesh Patil ji! ₹500 ka payment mil gaya. Aapka khata clear hai.")

    # The ledger entry records the UPI app and payment id.
    note = shop_db.execute("SELECT note FROM ledger WHERE type = 'payment'").fetchone()["note"]
    assert note == f"UPI (gpay) {json.loads(signed['payload'])['payment_id']}"


def test_webhook_is_idempotent(client, shop_db):
    owe(shop_db, 1, 500)
    _, signed, first = remind_and_pay(client, shop_db)

    # The gateway retries the exact same webhook (e.g. our 200 got lost).
    retry = client.post("/webhook/payment", content=signed["payload"],
                        headers={"X-Signature": signed["signature"]})
    assert retry.status_code == 200
    assert retry.json()["status"] == "already_processed"
    assert count(shop_db, "ledger", "type = 'payment'") == 1
    assert khata.get_balance(shop_db, 1) == 0


def test_simultaneous_duplicate_webhooks_credit_once(shop_db):
    """Two copies of the same webhook arriving at the same instant (gateway
    retry racing the original). BEGIN IMMEDIATE makes the second wait for the
    first, then see the payment_id and skip it."""
    owe(shop_db, 1, 500)
    token = payments.send_reminder(shop_db, "Ramesh")["token"]
    body = json.dumps({"event": "payment.captured", "payment_id": "pay_race12345678",
                       "token": token, "amount": 500, "method": "gpay"}).encode()
    signature = payments.sign(body)

    start = threading.Barrier(2)  # release both threads together
    results = []

    def deliver():
        with closing(get_connection()) as conn:  # each "request" has its own connection
            start.wait()
            results.append(payments.handle_webhook(conn, body, signature)["status"])

    threads = [threading.Thread(target=deliver) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == ["already_processed", "processed"]
    assert count(shop_db, "ledger", "type = 'payment'") == 1
    assert khata.get_balance(shop_db, 1) == 0


@pytest.mark.parametrize("headers", [{}, {"X-Signature": "not-a-real-signature"}])
def test_webhook_without_valid_signature_gives_401(client, shop_db, headers):
    owe(shop_db, 1, 500)
    token = payments.send_reminder(shop_db, "Ramesh")["token"]
    body = json.dumps({"event": "payment.captured", "payment_id": "pay_abcdef1234",
                       "token": token, "amount": 500, "method": "gpay"})
    r = client.post("/webhook/payment", content=body, headers=headers)
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_signature"
    assert khata.get_balance(shop_db, 1) == 500
    assert link_status(shop_db, token) == "pending"


def test_tampered_body_gives_401(client, shop_db):
    owe(shop_db, 1, 500)
    token = payments.send_reminder(shop_db, "Ramesh")["token"]
    signed = client.post(f"/gateway/checkout/{token}", json={"method": "gpay"}).json()

    tampered = signed["payload"].replace('"amount": 500', '"amount": 5')
    assert tampered != signed["payload"]
    r = client.post("/webhook/payment", content=tampered, headers={"X-Signature": signed["signature"]})
    assert r.status_code == 401


def signed_post(client, payload: dict):
    """Correctly signed webhook with arbitrary content (as if from the real gateway)."""
    body = json.dumps(payload).encode()
    return client.post("/webhook/payment", content=body, headers={"X-Signature": payments.sign(body)})


def test_signed_but_wrong_amount_gives_400(client, shop_db):
    owe(shop_db, 1, 500)
    token = payments.send_reminder(shop_db, "Ramesh")["token"]
    r = signed_post(client, {"event": "payment.captured", "payment_id": "pay_abcdef1234",
                             "token": token, "amount": 400, "method": "gpay"})
    assert r.status_code == 400
    assert khata.get_balance(shop_db, 1) == 500


def test_signed_but_unknown_token_gives_404(client):
    r = signed_post(client, {"event": "payment.captured", "payment_id": "pay_abcdef1234",
                             "token": "no-such-token", "amount": 500, "method": "gpay"})
    assert r.status_code == 404


def test_signed_but_malformed_body_gives_400(client):
    r = signed_post(client, {"event": "refund.created", "amount": 500})
    assert r.status_code == 400
    assert r.json()["error"] == "bad_payload"


def test_second_payment_for_paid_link_gives_409(client, shop_db):
    owe(shop_db, 1, 500)
    token, _, _ = remind_and_pay(client, shop_db)
    r = signed_post(client, {"event": "payment.captured", "payment_id": "pay_differentpay1",
                             "token": token, "amount": 500, "method": "paytm"})
    assert r.status_code == 409
    assert count(shop_db, "ledger", "type = 'payment'") == 1


def test_webhook_records_payment_even_if_cash_was_paid_meanwhile(client, shop_db):
    """Money already moved, so the ledger must record it (balance becomes an advance)."""
    owe(shop_db, 1, 500)
    token = payments.send_reminder(shop_db, "Ramesh")["token"]
    signed = client.post(f"/gateway/checkout/{token}", json={"method": "gpay"}).json()
    khata.add_payment(shop_db, "Ramesh", 200)  # cash, after checkout but before the webhook

    r = client.post("/webhook/payment", content=signed["payload"], headers={"X-Signature": signed["signature"]})
    assert r.status_code == 200
    assert khata.get_balance(shop_db, 1) == -200


# -------------------------------------------------------------- pay page ---

def test_pay_page_shows_amount_shop_and_buttons(client, shop_db, monkeypatch):
    monkeypatch.setenv("SHOP_NAME", "Sharma General Store")
    owe(shop_db, 1, 750)
    token = payments.send_reminder(shop_db, "Ramesh")["token"]

    r = client.get(f"/pay/{token}")
    assert r.status_code == 200
    page = r.text
    for text in ["₹750", "Sharma General Store", "Ramesh Patil", "Paytm", "Google Pay", "PhonePe", "X-Signature"]:
        assert text in page
    assert "test-secret" not in page  # the webhook secret must never reach the browser


def test_pay_page_after_payment_says_paid(client, shop_db):
    owe(shop_db, 1, 500)
    token, _, _ = remind_and_pay(client, shop_db)
    page = client.get(f"/pay/{token}").text
    assert "already paid" in page and "PhonePe" not in page
    assert client.post(f"/gateway/checkout/{token}", json={"method": "gpay"}).status_code == 409


def test_stale_link_cannot_be_paid(client, shop_db):
    owe(shop_db, 1, 500)
    token = payments.send_reminder(shop_db, "Ramesh")["token"]
    khata.add_payment(shop_db, "Ramesh", 300)  # paid cash; link for 500 is now too much

    assert "out of date" in client.get(f"/pay/{token}").text
    r = client.post(f"/gateway/checkout/{token}", json={"method": "gpay"})
    assert r.status_code == 409


def test_unknown_link_gives_404_page(client):
    r = client.get("/pay/does-not-exist")
    assert r.status_code == 404
    assert "not found" in r.text


def test_pay_page_escapes_customer_name(client, shop_db):
    shop_db.execute("INSERT INTO customers (name) VALUES ('<script>alert(1)</script>')")
    shop_db.commit()
    owe(shop_db, 4, 100)
    token = payments.send_reminder(shop_db, "<script>alert(1)</script>")["token"]
    page = client.get(f"/pay/{token}").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_checkout_rejects_unknown_method(client, shop_db):
    owe(shop_db, 1, 500)
    token = payments.send_reminder(shop_db, "Ramesh")["token"]
    assert client.post(f"/gateway/checkout/{token}", json={"method": "bitcoin"}).status_code == 422


@pytest.mark.parametrize("env, expected", [
    ({}, "http://127.0.0.1:8000"),
    ({"RENDER_EXTERNAL_URL": "https://udhaar.onrender.com"}, "https://udhaar.onrender.com"),
    ({"RENDER_EXTERNAL_URL": "https://udhaar.onrender.com", "PUBLIC_BASE_URL": "https://my.domain/"}, "https://my.domain"),
])
def test_base_url_for_payment_links(monkeypatch, env, expected):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert payments.base_url() == expected
