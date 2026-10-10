"""Trust score, FIFO settlement, and the /insights numbers."""

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import insights, khata
from app.main import app


# ---------------------------------------------------------------- helpers ---

def entry(conn, customer_id, type_, amount, days_ago, hour=12):
    conn.execute(
        "INSERT INTO ledger (customer_id, type, amount, created_at) VALUES (?, ?, ?, ?)",
        (customer_id, type_, amount, _days_ago(days_ago, hour)),
    )
    conn.commit()


def reminder(conn, customer_id, days_ago):
    conn.execute(
        "INSERT INTO messages (customer_id, direction, kind, body, created_at) VALUES (?, 'out', 'reminder', 'pay', ?)",
        (customer_id, _days_ago(days_ago)),
    )
    conn.commit()


def sale(conn, amount, when: datetime):
    conn.execute("INSERT INTO sales (amount, created_at) VALUES (?, ?)", (amount, when.strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()


def _days_ago(days, hour=12):
    return (datetime.now() - timedelta(days=days)).replace(hour=hour, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S")


def row(type_, amount, day):
    """Fake ledger row for fifo_settle tests."""
    return {"type": type_, "amount": amount, "created_at": f"2026-01-{day:02d} 10:00:00"}


# ------------------------------------------------------------ FIFO settle ---

def test_fifo_simple_repayment():
    days, oldest_open = khata.fifo_settle([row("udhaar", 500, 1), row("payment", 500, 4)])
    assert days == [3] and oldest_open is None


def test_fifo_payment_settles_oldest_first():
    rows = [row("udhaar", 300, 1), row("udhaar", 200, 5), row("payment", 300, 10)]
    days, oldest_open = khata.fifo_settle(rows)
    assert days == [9]                       # the Jan 1 udhaar took 9 days
    assert oldest_open == date(2026, 1, 5)   # the Jan 5 one is still open


def test_fifo_partial_payment_keeps_udhaar_open():
    days, oldest_open = khata.fifo_settle([row("udhaar", 500, 1), row("payment", 200, 3)])
    assert days == [] and oldest_open == date(2026, 1, 1)


def test_fifo_advance_covers_later_udhaar():
    rows = [row("udhaar", 100, 1), row("payment", 300, 2), row("udhaar", 200, 5)]
    days, oldest_open = khata.fifo_settle(rows)
    assert days == [1, 0] and oldest_open is None


# ------------------------------------------------------------ trust score ---

def test_new_customer_scores_100(shop_db):
    assert insights.trust_score(shop_db, 1) == 100


def test_quick_payer_scores_100(shop_db):
    entry(shop_db, 1, "udhaar", 500, days_ago=20)
    entry(shop_db, 1, "payment", 500, days_ago=17)   # repaid in 3 days (< 7 grace)
    assert insights.trust_score(shop_db, 1) == 100


def test_slow_repayment_penalty(shop_db):
    entry(shop_db, 1, "udhaar", 500, days_ago=40)
    entry(shop_db, 1, "payment", 500, days_ago=23)   # 17 days -> (17 - 7) * 1.5 = 15
    d = insights.trust_details(shop_db, 1)
    assert d["avg_repay_days"] == 17
    assert d["penalties"]["slow_repayment"] == 15
    assert d["score"] == 85


def test_overdue_penalty(shop_db):
    entry(shop_db, 1, "udhaar", 500, days_ago=27)    # (27 - 7) * 1.5 = 30
    d = insights.trust_details(shop_db, 1)
    assert d["days_overdue"] == 27
    assert d["score"] == 70


def test_reminder_penalty_only_counts_last_90_days(shop_db):
    reminder(shop_db, 1, days_ago=5)
    reminder(shop_db, 1, days_ago=60)
    reminder(shop_db, 1, days_ago=120)  # too old, ignored
    d = insights.trust_details(shop_db, 1)
    assert d["reminders_90d"] == 2
    assert d["score"] == 90


def test_thank_you_messages_are_not_reminders(shop_db):
    shop_db.execute("INSERT INTO messages (customer_id, direction, kind, body) VALUES (1, 'out', 'thank_you', 'thanks')")
    assert insights.trust_score(shop_db, 1) == 100


def test_penalties_are_capped_and_score_clamped(shop_db):
    entry(shop_db, 1, "udhaar", 500, days_ago=300)
    entry(shop_db, 1, "payment", 500, days_ago=200)  # 100 days to repay
    entry(shop_db, 1, "udhaar", 500, days_ago=150)   # 150 days overdue
    for d in range(10):
        reminder(shop_db, 1, days_ago=d + 1)
    details = insights.trust_details(shop_db, 1)
    assert details["penalties"] == {"slow_repayment": 30, "overdue": 40, "reminders": 30}
    assert details["score"] == 0


def test_seeded_habits_get_sensible_scores():
    from contextlib import closing
    from app.db import get_connection
    from app.seed import CUSTOMERS, seed

    seed()
    with closing(get_connection()) as conn:
        for i, c in enumerate(CUSTOMERS, start=1):
            score = insights.trust_score(conn, i)
            if c["habit"] == "punctual":
                assert score >= insights.TRUSTED_SCORE, (c["name"], score)
            if c["habit"] == "overdue":
                assert score < 50, (c["name"], score)


# ------------------------------------------------------------------ sales ---

def test_week_over_week(shop_db):
    now = datetime.now()
    sale(shop_db, 1500, now - timedelta(days=2))
    sale(shop_db, 1000, now - timedelta(days=10))
    sale(shop_db, 9999, now - timedelta(days=20))   # outside both windows
    assert insights.week_over_week(shop_db, now) == {"this_week": 1500, "last_week": 1000, "change_pct": 50.0}


def test_week_over_week_with_no_sales_last_week(shop_db):
    sale(shop_db, 500, datetime.now() - timedelta(hours=1))
    assert insights.week_over_week(shop_db, datetime.now())["change_pct"] is None


def test_daily_sales_fills_empty_days(shop_db):
    today = date.today()
    sale(shop_db, 100, datetime.now())
    sale(shop_db, 50, datetime.now())
    days = insights.daily_sales(shop_db, today)
    assert len(days) == 30
    assert days[0]["date"] == (today - timedelta(days=29)).isoformat()
    assert days[-1] == {"date": today.isoformat(), "total": 150, "bills": 2}
    assert days[-2]["total"] == 0


def test_peak_day_and_hour(shop_db):
    today = date.today()
    last_sunday = today - timedelta(days=(today.weekday() + 1) % 7 or 7)
    sale(shop_db, 5000, datetime.combine(last_sunday, datetime.min.time()).replace(hour=19))
    sale(shop_db, 100, datetime.combine(last_sunday - timedelta(days=1), datetime.min.time()).replace(hour=9))
    peak = insights.peak_times(shop_db, today)
    assert peak == {"day": "Sunday", "hour": 19, "hour_label": "7 PM - 8 PM"}


def test_peak_with_no_sales(shop_db):
    assert insights.peak_times(shop_db, date.today()) == {"day": None, "hour": None, "hour_label": None}


@pytest.mark.parametrize("hour, label", [(0, "12 AM"), (9, "9 AM"), (12, "12 PM"), (23, "11 PM"), (24, "12 AM")])
def test_clock(hour, label):
    assert insights._clock(hour) == label


# -------------------------------------------------------------- customers ---

def test_stuck_udhaar(shop_db):
    entry(shop_db, 1, "udhaar", 500, days_ago=20)
    entry(shop_db, 2, "udhaar", 300, days_ago=10)    # not stuck yet
    entry(shop_db, 3, "udhaar", 200, days_ago=15)    # exactly 15 -> stuck
    assert [c["name"] for c in insights.stuck_udhaar(shop_db)] == ["Ramesh Patil", "Suresh Gupta"]


def test_regulars_missing(shop_db):
    for days_ago in (8, 15, 22):                     # active in weeks 1, 2, 3 ...
        entry(shop_db, 1, "udhaar", 100, days_ago)   # ... but not this week -> missing
        entry(shop_db, 2, "udhaar", 100, days_ago)
    entry(shop_db, 2, "udhaar", 100, days_ago=2)     # Sunita came this week
    entry(shop_db, 3, "udhaar", 100, days_ago=10)    # Suresh: only 1 week, not a regular

    missing = insights.regulars_missing(shop_db, date.today())
    assert [m["name"] for m in missing] == ["Ramesh Patil"]
    assert missing[0]["days_since"] == 8


def test_trusted_customers(shop_db):
    entry(shop_db, 3, "udhaar", 500, days_ago=30)    # Suresh: 30 days overdue -> 65.5 -> not trusted
    trusted = insights.trusted_customers(shop_db)
    assert [c["name"] for c in trusted] == ["Ramesh Patil", "Sunita Deshmukh"]
    assert all(c["trust_score"] == 100 for c in trusted)


# -------------------------------------------------------------- endpoints ---

@pytest.fixture
def client(shop_db):
    with TestClient(app) as c:
        yield c


def test_khata_includes_trust_score(client, shop_db):
    entry(shop_db, 1, "udhaar", 500, days_ago=2)
    entry(shop_db, 3, "udhaar", 500, days_ago=27)
    customers = client.get("/khata").json()["customers"]
    assert {c["name"]: c["trust_score"] for c in customers} == {"Ramesh Patil": 100, "Suresh Gupta": 70}


def test_insights_endpoint_on_seeded_data(client):
    from app.seed import seed

    seed()
    body = client.get("/insights").json()
    assert set(body) == {"sales_week", "daily_sales", "stuck_udhaar", "regulars_missing", "trusted_customers", "peak"}
    assert len(body["daily_sales"]) == 30
    assert body["sales_week"]["this_week"] > 0
    assert {c["name"] for c in body["stuck_udhaar"]} >= {"Ganesh Jadhav", "Kavita Yadav", "Suresh Gupta"}
    assert all(c["trust_score"] >= 80 for c in body["trusted_customers"])
    assert body["peak"]["day"] in {"Saturday", "Sunday"}


# -------------------------------------------------------------- migration ---

def test_init_db_adds_kind_column_to_old_databases(temp_db):
    import sqlite3
    from app.db import init_db

    old = sqlite3.connect(temp_db)
    old.executescript("""
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL,
                               direction TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT);
        INSERT INTO customers (name) VALUES ('Ramesh');
        INSERT INTO messages (customer_id, direction, body) VALUES (1, 'out', 'old message');
    """)
    old.close()

    init_db()
    conn = sqlite3.connect(temp_db)
    assert conn.execute("SELECT kind, body FROM messages").fetchone() == ("other", "old message")
    conn.close()
