"""Khata (ledger) business logic.

Every function takes an open sqlite3 connection as its first argument. That
keeps this module independent of FastAPI: the HTTP endpoints in main.py and
the AI agent (a later step) call exactly the same functions, so the rules
below are enforced no matter who is calling.

Rules enforced here:
- Amounts are whole rupees, > 0 and <= MAX_AMOUNT.
- A payment can't be more than what the customer owes.
- Customer names are fuzzy-matched; unknown or ambiguous names raise errors
  instead of guessing.
- Balance is always computed from the ledger, never stored.
"""

import difflib
import re
import sqlite3
from collections import deque
from datetime import date, datetime

# Upper limit per entry. Voice input can mishear "500" as "50000"; a kirana
# udhaar above Rs 1 lakh is almost certainly a mistake, so we refuse it.
MAX_AMOUNT = 100_000

# Fuzzy matching thresholds (difflib similarity ratio, 0.0 - 1.0).
# 0.85 accepts a missing/extra letter ("Ramsh", "Sunitha" -> 0.91) but rejects
# a swapped letter in a short name ("Rajesh" vs "Ramesh" -> 0.83), because
# that is usually a different person, not a typo.
MATCH_CUTOFF = 0.85      # at or above this, we treat the name as a match
SUGGEST_CUTOFF = 0.6     # at or above this, we suggest it in "did you mean?"
AMBIGUITY_MARGIN = 0.1   # if two matches score within this of each other, ask

# Respectful words people add to names when speaking ("Ramesh ji", "Sunita tai").
HONORIFICS = {"ji", "bhai", "bhaiya", "bhau", "didi", "tai", "kaka", "kaki",
              "seth", "saheb", "sir", "madam", "uncle", "aunty"}

# +amount for udhaar (customer owes more), -amount for payment (owes less).
SIGNED_AMOUNT = "CASE type WHEN 'udhaar' THEN amount ELSE -amount END"


# ---------------------------------------------------------------- errors ---

class KhataError(Exception):
    """Base class for errors the user can fix. main.py turns these into HTTP responses."""

    code = "khata_error"

    def __init__(self, message: str, **extra):
        super().__init__(message)
        self.message = message
        self.extra = extra  # additional JSON fields, e.g. suggestions


class CustomerNotFound(KhataError):
    code = "customer_not_found"


class AmbiguousCustomer(KhataError):
    code = "ambiguous_customer"


class InvalidAmount(KhataError):
    code = "invalid_amount"


# --------------------------------------------------------- name matching ---

def normalize_name(name: str) -> str:
    """'  Ramesh-ji! ' -> 'ramesh'. Lowercase, drop punctuation and honorifics."""
    words = re.sub(r"[.,!?'\"\-]", " ", name.lower()).split()
    return " ".join(w for w in words if w not in HONORIFICS)


def _similarity(query: str, customer_name: str) -> float:
    """Best similarity of the query against the full name or any single word of it.

    Comparing against single words lets "Ramesh" match "Ramesh Patil" fully,
    since shop owners usually say only the first name.
    """
    full = normalize_name(customer_name)
    options = [full] + full.split()
    return max(difflib.SequenceMatcher(None, query, option).ratio() for option in options)


def find_customer(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    """Return the one customer matching `name`, or raise a clear error.

    Wrongly assigning udhaar to the wrong person is worse than asking again,
    so we only return a match when it is clearly the best one.
    """
    query = normalize_name(name)
    if not query:
        raise CustomerNotFound("Customer name is empty.")

    customers = conn.execute("SELECT id, name, phone FROM customers").fetchall()
    scored = sorted(
        ((_similarity(query, c["name"]), c) for c in customers),
        key=lambda pair: pair[0],
        reverse=True,
    )

    matches = [(score, c) for score, c in scored if score >= MATCH_CUTOFF]
    if not matches:
        suggestions = [c["name"] for score, c in scored if score >= SUGGEST_CUTOFF][:3]
        raise CustomerNotFound(f"No customer named '{name}'.", suggestions=suggestions)

    best_score, best = matches[0]
    close_calls = [c["name"] for score, c in matches if best_score - score < AMBIGUITY_MARGIN]
    if len(close_calls) > 1:
        raise AmbiguousCustomer(
            f"'{name}' matches more than one customer. Please say the full name.",
            candidates=close_calls,
        )
    return best


# ------------------------------------------------------- balance helpers ---

def get_balance(conn: sqlite3.Connection, customer_id: int) -> int:
    """How much the customer owes right now, computed from the ledger."""
    return conn.execute(
        f"SELECT COALESCE(SUM({SIGNED_AMOUNT}), 0) FROM ledger WHERE customer_id = ?",
        (customer_id,),
    ).fetchone()[0]


def ledger_rows(conn: sqlite3.Connection, customer_id: int) -> list[sqlite3.Row]:
    """A customer's ledger in time order."""
    return conn.execute(
        "SELECT type, amount, created_at FROM ledger WHERE customer_id = ? ORDER BY created_at, id",
        (customer_id,),
    ).fetchall()


def _to_date(timestamp: str) -> date:
    return datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S").date()


def fifo_settle(rows: list[sqlite3.Row]) -> tuple[list[int], date | None]:
    """Match payments to udhaar entries, oldest udhaar first (FIFO).

    Payments aren't linked to specific udhaar entries, so we assume every
    payment settles the oldest dues first. That's how a shopkeeper thinks
    about it too ("purana pehle chukta hota hai": old dues get paid first).

    Returns:
        repay_days:  for each fully settled udhaar, how many days it took to repay
        oldest_open: date of the oldest udhaar not yet fully paid (None if all paid)
    """
    open_items: deque[list] = deque()  # [udhaar_date, amount_still_unpaid], oldest first
    advance = 0                        # money paid beyond what was owed
    repay_days: list[int] = []

    for r in rows:
        day = _to_date(r["created_at"])
        if r["type"] == "udhaar":
            covered = min(advance, r["amount"])  # an earlier advance pays this immediately
            advance -= covered
            if covered == r["amount"]:
                repay_days.append(0)
            else:
                open_items.append([day, r["amount"] - covered])
        else:
            money = r["amount"]
            while money > 0 and open_items:
                item = open_items[0]
                used = min(money, item[1])
                item[1] -= used
                money -= used
                if item[1] == 0:                      # this udhaar is now fully paid
                    repay_days.append((day - item[0]).days)
                    open_items.popleft()
            advance += money

    return repay_days, (open_items[0][0] if open_items else None)


def days_pending(conn: sqlite3.Connection, customer_id: int, today: date | None = None) -> int | None:
    """Age in days of the oldest udhaar that is not yet fully paid, or None."""
    _, oldest_open = fifo_settle(ledger_rows(conn, customer_id))
    if oldest_open is None:
        return None
    return ((today or date.today()) - oldest_open).days


def _validate_amount(amount: int) -> None:
    # bool is a subclass of int in Python (True == 1), so reject it explicitly.
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise InvalidAmount("Amount must be a whole number of rupees.")
    if amount <= 0:
        raise InvalidAmount("Amount must be greater than 0.")
    if amount > MAX_AMOUNT:
        raise InvalidAmount(f"Amount cannot be more than Rs {MAX_AMOUNT}. Please check the amount.")


def _insert_entry(conn: sqlite3.Connection, customer_id: int, type_: str, amount: int, note: str | None) -> dict:
    cur = conn.execute(
        "INSERT INTO ledger (customer_id, type, amount, note) VALUES (?, ?, ?, ?)",
        (customer_id, type_, amount, note),
    )
    row = conn.execute(
        "SELECT id, type, amount, note, created_at FROM ledger WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    return {**dict(row), "balance_after": get_balance(conn, customer_id)}


# ------------------------------------------------------------- commands ---

def add_udhaar(conn: sqlite3.Connection, customer_name: str, amount: int, note: str | None = None) -> dict:
    """Record credit given to a customer."""
    _validate_amount(amount)
    # 'with conn' commits on success and rolls back if anything raises.
    with conn:
        customer = find_customer(conn, customer_name)
        entry = _insert_entry(conn, customer["id"], "udhaar", amount, note)

    summary = customer_summary(conn, customer["id"])
    return {
        "message": f"Added Rs {amount} udhaar for {customer['name']}. Total pending: Rs {summary['balance']}.",
        "customer": summary,
        "entry": entry,
    }


def add_payment(conn: sqlite3.Connection, customer_name: str, amount: int, note: str | None = None) -> dict:
    """Record money received from a customer."""
    _validate_amount(amount)
    with conn:
        # BEGIN IMMEDIATE takes the write lock *before* we read the balance.
        # Without it, two payments arriving at the same moment could both see
        # "owes 500", both pass the check, and leave the balance at -500.
        conn.execute("BEGIN IMMEDIATE")
        customer = find_customer(conn, customer_name)
        balance = get_balance(conn, customer["id"])
        if balance == 0:
            raise InvalidAmount(f"{customer['name']} has no pending udhaar.", balance=0, customer=customer["name"])
        if amount > balance:
            raise InvalidAmount(
                f"Payment of Rs {amount} is more than {customer['name']}'s pending Rs {balance}.",
                balance=balance, customer=customer["name"],
            )
        entry = _insert_entry(conn, customer["id"], "payment", amount, note)

    summary = customer_summary(conn, customer["id"])
    status = "Khata cleared!" if summary["balance"] == 0 else f"Still pending: Rs {summary['balance']}."
    return {
        "message": f"Received Rs {amount} from {customer['name']}. {status}",
        "customer": summary,
        "entry": entry,
    }


def record_received_payment(conn: sqlite3.Connection, customer_id: int, amount: int, note: str) -> dict:
    """Record money that has ALREADY been received (e.g. a verified UPI webhook).

    Unlike add_payment, this does not refuse an amount above the balance:
    the money is already in the shop's account, and the ledger must record
    what actually happened. A negative balance then means an advance.
    The caller manages the transaction.
    """
    _validate_amount(amount)
    return _insert_entry(conn, customer_id, "payment", amount, note)


# -------------------------------------------------------------- queries ---

def customer_summary(conn: sqlite3.Connection, customer_id: int) -> dict:
    row = conn.execute("SELECT id, name, phone FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if row is None:
        raise CustomerNotFound(f"No customer with id {customer_id}.")
    return {
        **dict(row),
        "balance": get_balance(conn, customer_id),
        "days_pending": days_pending(conn, customer_id),
    }


def list_pending(conn: sqlite3.Connection) -> list[dict]:
    """Customers who owe money, biggest balance first."""
    rows = conn.execute(
        f"""
        SELECT c.id, c.name, c.phone, SUM({SIGNED_AMOUNT}) AS balance
        FROM customers c
        JOIN ledger l ON l.customer_id = c.id
        GROUP BY c.id
        HAVING balance > 0
        ORDER BY balance DESC, c.name
        """
    ).fetchall()
    # One extra query per customer for days_pending. Fine for a kirana's few
    # hundred customers; at larger scale we'd compute it in a single pass.
    return [{**dict(r), "days_pending": days_pending(conn, r["id"])} for r in rows]


def get_customer_detail(conn: sqlite3.Connection, customer_id: int) -> dict:
    """Customer summary plus full ledger history with a running balance."""
    summary = customer_summary(conn, customer_id)
    rows = conn.execute(
        "SELECT id, type, amount, note, created_at FROM ledger WHERE customer_id = ? ORDER BY created_at, id",
        (customer_id,),
    ).fetchall()

    ledger, running = [], 0
    for r in rows:
        running += r["amount"] if r["type"] == "udhaar" else -r["amount"]
        ledger.append({**dict(r), "balance_after": running})
    return {**summary, "ledger": ledger}
