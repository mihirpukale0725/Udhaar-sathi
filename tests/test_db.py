import sqlite3
from contextlib import closing

import pytest

from app.db import get_connection, init_db


@pytest.fixture
def conn():
    init_db()
    with closing(get_connection()) as c:
        c.execute("INSERT INTO customers (name, phone) VALUES ('Ramesh', '+919800000001')")
        yield c


def test_all_tables_created(conn):
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"customers", "ledger", "payment_links", "messages", "sales"} <= tables


def test_init_db_is_idempotent():
    init_db()
    init_db()  # must not raise "table already exists"


@pytest.mark.parametrize("amount", [0, -100])
def test_ledger_rejects_non_positive_amount(conn, amount):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO ledger (customer_id, type, amount) VALUES (1, 'udhaar', ?)", (amount,))


def test_ledger_rejects_unknown_type(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO ledger (customer_id, type, amount) VALUES (1, 'refund', 100)")


def test_foreign_keys_are_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO ledger (customer_id, type, amount) VALUES (999, 'udhaar', 100)")


def test_payment_id_is_unique(conn):
    """The same gateway payment_id can't be recorded twice (webhook idempotency)."""
    conn.execute("INSERT INTO payment_links (token, customer_id, amount, status, payment_id) VALUES ('t1', 1, 100, 'paid', 'pay_1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO payment_links (token, customer_id, amount, status, payment_id) VALUES ('t2', 1, 100, 'paid', 'pay_1')")


def test_balance_is_sum_of_ledger(conn):
    conn.executemany(
        "INSERT INTO ledger (customer_id, type, amount) VALUES (1, ?, ?)",
        [("udhaar", 500), ("udhaar", 200), ("payment", 300)],
    )
    balance = conn.execute(
        "SELECT SUM(CASE type WHEN 'udhaar' THEN amount ELSE -amount END) FROM ledger WHERE customer_id = 1"
    ).fetchone()[0]
    assert balance == 400
