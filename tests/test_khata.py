"""Unit tests for khata.py: business rules, no HTTP involved."""

from datetime import date

import pytest

from app import khata
from app.khata import AmbiguousCustomer, CustomerNotFound, InvalidAmount


# --------------------------------------------------------- name matching ---

@pytest.mark.parametrize("spoken", [
    "Ramesh Patil",     # exact
    "ramesh",           # first name only, lowercase
    "RAMESH",           # shouting
    "Ramesh ji",        # honorific
    "ramesh-bhai!",     # punctuation + honorific
    "Ramsh",            # speech-to-text dropped a letter
    "Patil",            # surname only
    "  ramesh patil  ",
])
def test_find_customer_fuzzy_matches(shop_db, spoken):
    assert khata.find_customer(shop_db, spoken)["name"] == "Ramesh Patil"


def test_similar_but_different_name_is_not_matched(shop_db):
    # "Rajesh" is a different person, not a typo: it must NOT silently become Ramesh.
    with pytest.raises(CustomerNotFound) as exc:
        khata.find_customer(shop_db, "Rajesh")
    assert "Ramesh Patil" in exc.value.extra["suggestions"]


def test_unknown_name_has_no_suggestions(shop_db):
    with pytest.raises(CustomerNotFound) as exc:
        khata.find_customer(shop_db, "Xavier")
    assert exc.value.extra["suggestions"] == []


def test_ambiguous_first_name_asks_instead_of_guessing(shop_db):
    shop_db.execute("INSERT INTO customers (name) VALUES ('Ramesh Gupta')")
    with pytest.raises(AmbiguousCustomer) as exc:
        khata.find_customer(shop_db, "Ramesh")
    assert set(exc.value.extra["candidates"]) == {"Ramesh Patil", "Ramesh Gupta"}
    # The full name resolves it.
    assert khata.find_customer(shop_db, "Ramesh Gupta")["name"] == "Ramesh Gupta"


# ---------------------------------------------------------------- amounts ---

@pytest.mark.parametrize("amount", [0, -100, khata.MAX_AMOUNT + 1, 50.5, True])
def test_invalid_amounts_are_rejected(shop_db, amount):
    with pytest.raises(InvalidAmount):
        khata.add_udhaar(shop_db, "Ramesh", amount)
    assert khata.get_balance(shop_db, 1) == 0  # nothing was written


# ------------------------------------------------------- udhaar / payment ---

def test_udhaar_then_payment_updates_balance(shop_db):
    result = khata.add_udhaar(shop_db, "Ramesh", 500, "atta")
    assert result["customer"]["balance"] == 500
    assert result["entry"]["balance_after"] == 500

    result = khata.add_payment(shop_db, "Ramesh", 200)
    assert result["customer"]["balance"] == 300
    assert "Still pending" in result["message"]

    result = khata.add_payment(shop_db, "Ramesh", 300)
    assert result["customer"]["balance"] == 0
    assert "cleared" in result["message"]


def test_payment_with_nothing_owed_is_rejected(shop_db):
    with pytest.raises(InvalidAmount, match="no pending"):
        khata.add_payment(shop_db, "Sunita", 100)


def test_overpayment_is_rejected_and_rolled_back(shop_db):
    khata.add_udhaar(shop_db, "Ramesh", 500)
    with pytest.raises(InvalidAmount) as exc:
        khata.add_payment(shop_db, "Ramesh", 501)
    assert exc.value.extra["balance"] == 500
    assert khata.get_balance(shop_db, 1) == 500


def test_balance_is_not_stored_on_customers(shop_db):
    columns = {row["name"] for row in shop_db.execute("PRAGMA table_info(customers)")}
    assert "balance" not in columns


# ----------------------------------------------------------- days pending ---

def _add(conn, type_, amount, created_at):
    conn.execute(
        "INSERT INTO ledger (customer_id, type, amount, created_at) VALUES (1, ?, ?, ?)",
        (type_, amount, created_at),
    )


def test_days_pending_uses_fifo(shop_db):
    today = date(2026, 1, 20)
    _add(shop_db, "udhaar", 300, "2026-01-01 10:00:00")
    _add(shop_db, "udhaar", 200, "2026-01-10 10:00:00")
    assert khata.days_pending(shop_db, 1, today) == 19     # Jan 1 is oldest unpaid

    _add(shop_db, "payment", 300, "2026-01-15 10:00:00")  # settles the Jan 1 udhaar
    assert khata.days_pending(shop_db, 1, today) == 10     # now Jan 10 is oldest

    _add(shop_db, "payment", 100, "2026-01-16 10:00:00")  # partial: Jan 10 still open
    assert khata.days_pending(shop_db, 1, today) == 10

    _add(shop_db, "payment", 100, "2026-01-17 10:00:00")  # all paid
    assert khata.days_pending(shop_db, 1, today) is None


# ---------------------------------------------------------------- queries ---

def test_list_pending_sorted_by_balance_and_skips_cleared(shop_db):
    khata.add_udhaar(shop_db, "Ramesh", 300)
    khata.add_udhaar(shop_db, "Suresh", 900)
    khata.add_udhaar(shop_db, "Sunita", 100)
    khata.add_payment(shop_db, "Sunita", 100)  # Sunita is fully paid

    pending = khata.list_pending(shop_db)
    assert [c["name"] for c in pending] == ["Suresh Gupta", "Ramesh Patil"]
    assert pending[0]["days_pending"] == 0


def test_customer_detail_has_running_balance(shop_db):
    khata.add_udhaar(shop_db, "Ramesh", 500)
    khata.add_udhaar(shop_db, "Ramesh", 250)
    khata.add_payment(shop_db, "Ramesh", 600)

    detail = khata.get_customer_detail(shop_db, 1)
    assert detail["balance"] == 150
    assert [e["balance_after"] for e in detail["ledger"]] == [500, 750, 150]


def test_customer_detail_unknown_id(shop_db):
    with pytest.raises(CustomerNotFound):
        khata.get_customer_detail(shop_db, 999)
