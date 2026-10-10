"""SQLite database layer (plain sqlite3, no ORM).

Key rule: a customer's balance is NEVER stored. It is always computed as
    SUM(udhaar amounts) - SUM(payment amounts)
from the ledger table. The ledger is append-only, so the balance can never
drift out of sync with the history that produced it.

All money is stored as INTEGER rupees. Kirana udhaar is in whole rupees, and
integers avoid floating-point rounding errors (0.1 + 0.2 != 0.3).
"""

import os
import sqlite3
from contextlib import closing

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL,
    phone TEXT                      -- optional: owner may add a customer by name only
);

-- Append-only khata. Every udhaar (credit given) and payment is one row.
-- amount is always positive; 'type' decides whether it adds to or reduces the balance.
CREATE TABLE IF NOT EXISTS ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    type        TEXT    NOT NULL CHECK (type IN ('udhaar', 'payment')),
    amount      INTEGER NOT NULL CHECK (amount > 0),
    note        TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_ledger_customer ON ledger(customer_id);

-- One row per UPI payment link we send. 'token' is the unguessable id in /pay/{token}.
-- payment_id is UNIQUE so the same gateway payment can never be applied twice
-- (this is what makes the webhook idempotent at the database level).
CREATE TABLE IF NOT EXISTS payment_links (
    token       TEXT    PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    amount      INTEGER NOT NULL CHECK (amount > 0),
    status      TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'paid')),
    payment_id  TEXT    UNIQUE,     -- NULL until paid; SQLite allows many NULLs in a UNIQUE column
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Mock WhatsApp. 'out' = shop -> customer, 'in' = customer -> shop.
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    direction   TEXT    NOT NULL CHECK (direction IN ('out', 'in')),
    body        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Fake UPI sales (all walk-in sales, not just udhaar). Used for business insights.
CREATE TABLE IF NOT EXISTS sales (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    amount     INTEGER NOT NULL CHECK (amount > 0),
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sales_created ON sales(created_at);
"""


def get_db_path() -> str:
    """Read the DB location at call time so tests can point it at a temp file."""
    return os.getenv("DB_PATH", "udhaar.db")


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    """Open a new connection with the settings every caller needs."""
    # check_same_thread=False: FastAPI may open the connection (in a dependency)
    # on one worker thread and use it on another. That's safe here because each
    # request gets its own connection and uses it one step at a time.
    conn = sqlite3.connect(db_path or get_db_path(), check_same_thread=False)
    # Rows behave like dicts: row["name"] instead of row[1].
    conn.row_factory = sqlite3.Row
    # SQLite does NOT enforce foreign keys unless you turn them on per connection.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | None = None) -> None:
    """Create all tables if they don't exist. Safe to call repeatedly."""
    # closing() guarantees the connection is closed even if the script fails.
    with closing(get_connection(db_path)) as conn:
        conn.executescript(SCHEMA)
        conn.commit()
