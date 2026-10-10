from contextlib import closing

import pytest

from app.db import get_connection
from app.khata import days_pending
from app.seed import CUSTOMERS, SALES_DAYS, seed


@pytest.fixture
def conn():
    seed()
    with closing(get_connection()) as c:
        yield c


def test_seeds_eight_customers(conn):
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 8


def test_seed_is_rerunnable(conn):
    seed()  # second run wipes and re-inserts, so counts don't double
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 8


def test_overdue_customers_are_20_to_35_days_overdue(conn):
    for i, c in enumerate(CUSTOMERS, start=1):
        if c["habit"] == "overdue":
            age = days_pending(conn, i)
            assert age is not None and 20 <= age <= 35, (c["name"], age)


def test_punctual_customers_have_nothing_old_unpaid(conn):
    for i, c in enumerate(CUSTOMERS, start=1):
        if c["habit"] == "punctual":
            age = days_pending(conn, i)
            assert age is None or age <= 5, (c["name"], age)


def test_no_customer_has_negative_balance(conn):
    rows = conn.execute(
        "SELECT customer_id, SUM(CASE type WHEN 'udhaar' THEN amount ELSE -amount END) AS bal "
        "FROM ledger GROUP BY customer_id"
    ).fetchall()
    assert all(r["bal"] >= 0 for r in rows)


def test_sales_cover_30_days_within_shop_hours(conn):
    past_days = conn.execute(
        "SELECT COUNT(DISTINCT date(created_at)) FROM sales WHERE date(created_at) < date('now', 'localtime')"
    ).fetchone()[0]
    assert past_days == SALES_DAYS
    in_future = conn.execute("SELECT COUNT(*) FROM sales WHERE created_at > datetime('now', 'localtime')").fetchone()[0]
    assert in_future == 0
    hours = {r[0] for r in conn.execute("SELECT DISTINCT CAST(strftime('%H', created_at) AS INTEGER) FROM sales")}
    assert min(hours) >= 7 and max(hours) <= 21


def test_sunday_evening_is_busier_than_weekday_evening(conn):
    # strftime('%w'): 0 = Sunday ... 6 = Saturday. Compare average sales per evening (5-10 PM).
    # Today is excluded because its evening may not have happened yet.
    def avg_evening_sales(weekday_condition: str) -> float:
        return conn.execute(
            f"""
            SELECT AVG(n) FROM (
                SELECT date(created_at) AS d, COUNT(*) AS n FROM sales
                WHERE CAST(strftime('%H', created_at) AS INTEGER) >= 17
                  AND {weekday_condition}
                  AND date(created_at) < date('now', 'localtime')
                GROUP BY d
            )
            """
        ).fetchone()[0]

    sunday = avg_evening_sales("strftime('%w', created_at) = '0'")
    weekday = avg_evening_sales("strftime('%w', created_at) BETWEEN '1' AND '5'")
    assert sunday > weekday * 1.5
