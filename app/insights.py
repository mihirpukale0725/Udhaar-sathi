"""Business insights and the customer trust score.

TRUST SCORE (0-100), in plain words
-----------------------------------
Every customer starts at 100 points. We then take points away for three
habits, each with a maximum so no single habit can wipe out the score:

1. Slow repayment (up to -30)
   How many days, on average, did they take to pay back past udhaar?
   The first 7 days are free (a week is normal for a kirana khata).
   After that: -1.5 points per extra day.
   e.g. pays back in 20 days on average -> (20 - 7) x 1.5 = -19.5

2. Currently overdue (up to -40)
   How old is their oldest udhaar that is still unpaid?
   Again the first 7 days are free, then -1.5 points per day.
   This weighs the most because it is money the shop is missing right now.
   e.g. oldest unpaid udhaar is 28 days old -> (28 - 7) x 1.5 = -31.5

3. Reminders needed (up to -30)
   -5 points for every reminder sent in the last 90 days.
   e.g. 3 reminders -> -15

The result is rounded and clamped to 0-100.
  >= 80   trusted: safe to give more udhaar
  50-79   okay: keep an eye on them
  < 50    risky: collect before giving more

A customer with no history keeps 100: there is no evidence against them yet.
Known limitation: "sabko reminder bhejo" counts against everyone who had a
balance at that moment, even people who were not late.

Payments are matched to udhaar oldest-first (FIFO, see khata.fifo_settle),
which is how "days taken to repay" is measured.
"""

import sqlite3
from datetime import date, datetime, timedelta
from statistics import mean

from app import khata
from app.khata import SIGNED_AMOUNT

# --- trust score settings (see the explanation above)
GRACE_DAYS = 7
POINTS_PER_LATE_DAY = 1.5
MAX_SLOW_PENALTY = 30
MAX_OVERDUE_PENALTY = 40
POINTS_PER_REMINDER = 5
MAX_REMINDER_PENALTY = 30
REMINDER_WINDOW_DAYS = 90
TRUSTED_SCORE = 80

# --- insight settings
STUCK_AFTER_DAYS = 15        # udhaar this old counts as "stuck"
REGULAR_MIN_WEEKS = 3        # active in >= 3 of the 4 weeks before this one = a regular

WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]  # SQLite %w order

# How many days *before today* each business_summary period includes.
PERIOD_DAYS = {"today": 0, "week": 6, "month": 29}


def _ts(moment: datetime) -> str:
    """Format like our stored timestamps, so string comparison works."""
    return moment.strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------ trust score ---

def trust_details(conn: sqlite3.Connection, customer_id: int, today: date | None = None) -> dict:
    """The trust score plus every number that went into it (useful for showing 'why')."""
    today = today or date.today()

    repay_days, oldest_open = khata.fifo_settle(khata.ledger_rows(conn, customer_id))
    avg_repay = mean(repay_days) if repay_days else None
    days_overdue = (today - oldest_open).days if oldest_open else 0

    since = (today - timedelta(days=REMINDER_WINDOW_DAYS)).isoformat()
    reminders = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE customer_id = ? AND kind = 'reminder' AND created_at >= ?",
        (customer_id, since),
    ).fetchone()[0]

    slow = 0.0 if avg_repay is None else max(0.0, avg_repay - GRACE_DAYS) * POINTS_PER_LATE_DAY
    overdue = max(0, days_overdue - GRACE_DAYS) * POINTS_PER_LATE_DAY
    nagging = reminders * POINTS_PER_REMINDER

    penalties = {
        "slow_repayment": round(min(slow, MAX_SLOW_PENALTY), 1),
        "overdue": round(min(overdue, MAX_OVERDUE_PENALTY), 1),
        "reminders": min(nagging, MAX_REMINDER_PENALTY),
    }
    score = round(100 - sum(penalties.values()))
    return {
        "score": max(0, min(100, score)),
        "avg_repay_days": None if avg_repay is None else round(avg_repay, 1),
        "days_overdue": days_overdue,
        "reminders_90d": reminders,
        "penalties": penalties,
    }


def trust_score(conn: sqlite3.Connection, customer_id: int) -> int:
    """0-100. See the module docstring for the formula."""
    return trust_details(conn, customer_id)["score"]


# ------------------------------------------------------------------ sales ---

def _sales_between(conn: sqlite3.Connection, start: datetime, end: datetime) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM sales WHERE created_at > ? AND created_at <= ?",
        (_ts(start), _ts(end)),
    ).fetchone()[0]


def week_over_week(conn: sqlite3.Connection, now: datetime) -> dict:
    """Last 7 days vs the 7 days before that.

    Rolling windows that end *right now*: comparing "Monday till now" with
    a full previous week would make every week look worse until Sunday night.
    """
    this_week = _sales_between(conn, now - timedelta(days=7), now)
    last_week = _sales_between(conn, now - timedelta(days=14), now - timedelta(days=7))
    change = None if last_week == 0 else round((this_week - last_week) / last_week * 100, 1)
    return {"this_week": this_week, "last_week": last_week, "change_pct": change}


def daily_sales(conn: sqlite3.Connection, today: date, days: int = 30) -> list[dict]:
    """One entry per day for the last `days` days (oldest first), including zero-sale days."""
    start = today - timedelta(days=days - 1)
    rows = conn.execute(
        "SELECT date(created_at) AS day, SUM(amount) AS total, COUNT(*) AS bills "
        "FROM sales WHERE created_at >= ? GROUP BY day",
        (start.isoformat(),),
    ).fetchall()
    by_day = {r["day"]: r for r in rows}

    result = []
    for i in range(days):
        day = (start + timedelta(days=i)).isoformat()
        r = by_day.get(day)
        result.append({"date": day, "total": r["total"] if r else 0, "bills": r["bills"] if r else 0})
    return result


def peak_times(conn: sqlite3.Connection, today: date, days: int = 30) -> dict:
    """Busiest weekday (average sales per day) and busiest hour (total sales)."""
    since = (today - timedelta(days=days - 1)).isoformat()

    # Average, not total: in 30 days some weekdays occur 5 times and others 4.
    day = conn.execute(
        "SELECT CAST(strftime('%w', created_at) AS INTEGER) AS weekday, "
        "       SUM(amount) * 1.0 / COUNT(DISTINCT date(created_at)) AS avg_sales "
        "FROM sales WHERE created_at >= ? GROUP BY weekday ORDER BY avg_sales DESC LIMIT 1",
        (since,),
    ).fetchone()
    hour = conn.execute(
        "SELECT CAST(strftime('%H', created_at) AS INTEGER) AS hour, SUM(amount) AS total "
        "FROM sales WHERE created_at >= ? GROUP BY hour ORDER BY total DESC LIMIT 1",
        (since,),
    ).fetchone()

    return {
        "day": WEEKDAYS[day["weekday"]] if day else None,
        "hour": hour["hour"] if hour else None,
        "hour_label": f"{_clock(hour['hour'])} - {_clock(hour['hour'] + 1)}" if hour else None,
    }


def _clock(hour: int) -> str:
    """19 -> '7 PM', 0 or 24 -> '12 AM'."""
    hour %= 24
    return f"{hour % 12 or 12} {'AM' if hour < 12 else 'PM'}"


# -------------------------------------------------------------- customers ---

def stuck_udhaar(conn: sqlite3.Connection) -> list[dict]:
    """Customers whose oldest unpaid udhaar is STUCK_AFTER_DAYS or older, oldest first."""
    stuck = [c for c in khata.list_pending(conn) if (c["days_pending"] or 0) >= STUCK_AFTER_DAYS]
    return sorted(stuck, key=lambda c: c["days_pending"], reverse=True)


def regulars_missing(conn: sqlite3.Connection, today: date) -> list[dict]:
    """Regular customers who haven't come in this week.

    "Came in" = any khata entry (udhaar or payment). A regular is someone
    active in at least REGULAR_MIN_WEEKS of the 4 weeks before this one.
    Missing regulars may have switched shops; worth a friendly call.
    """
    since = (today - timedelta(days=34)).isoformat()  # this week + 4 earlier weeks
    rows = conn.execute(
        "SELECT c.id, c.name, l.created_at FROM ledger l JOIN customers c ON c.id = l.customer_id "
        "WHERE l.created_at >= ?",
        (since,),
    ).fetchall()

    weeks: dict[int, set[int]] = {}      # customer id -> week numbers active (0 = last 7 days)
    last_seen: dict[int, date] = {}
    names: dict[int, str] = {}
    for r in rows:
        day = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S").date()
        weeks.setdefault(r["id"], set()).add((today - day).days // 7)
        last_seen[r["id"]] = max(last_seen.get(r["id"], day), day)
        names[r["id"]] = r["name"]

    missing = []
    for cid, active in weeks.items():
        earlier_weeks = len(active & {1, 2, 3, 4})
        if earlier_weeks >= REGULAR_MIN_WEEKS and 0 not in active:
            missing.append({
                "id": cid,
                "name": names[cid],
                "last_seen": last_seen[cid].isoformat(),
                "days_since": (today - last_seen[cid]).days,
            })
    return sorted(missing, key=lambda m: m["days_since"], reverse=True)


def trusted_customers(conn: sqlite3.Connection) -> list[dict]:
    """Everyone with a trust score of TRUSTED_SCORE or more, best first."""
    customers = conn.execute("SELECT id, name FROM customers").fetchall()
    scored = [{"id": c["id"], "name": c["name"], "trust_score": trust_score(conn, c["id"])} for c in customers]
    trusted = [c for c in scored if c["trust_score"] >= TRUSTED_SCORE]
    return sorted(trusted, key=lambda c: (-c["trust_score"], c["name"]))


# --------------------------------------------------------------- combined ---

def get_insights(conn: sqlite3.Connection) -> dict:
    """Everything the dashboard needs, in one call."""
    now = datetime.now()
    today = now.date()
    return {
        "sales_week": week_over_week(conn, now),
        "daily_sales": daily_sales(conn, today),
        "stuck_udhaar": stuck_udhaar(conn),
        "regulars_missing": regulars_missing(conn, today),
        "trusted_customers": trusted_customers(conn),
        "peak": peak_times(conn, today),
    }


def business_summary(conn: sqlite3.Connection, period: str = "today") -> dict:
    """UPI sales, udhaar given, and udhaar collected in the period, plus total still pending.

    Used by the voice agent ("aaj kitna aaya?").
    """
    if period not in PERIOD_DAYS:
        raise ValueError(f"period must be one of {list(PERIOD_DAYS)}")

    # Timestamps are stored as 'YYYY-MM-DD HH:MM:SS' text, so comparing against
    # 'YYYY-MM-DD' works as a plain string comparison (and can use the index).
    start = (date.today() - timedelta(days=PERIOD_DAYS[period])).isoformat()

    sales_count, sales_total = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM sales WHERE created_at >= ?", (start,)
    ).fetchone()

    totals = dict(conn.execute(
        "SELECT type, SUM(amount) FROM ledger WHERE created_at >= ? GROUP BY type", (start,)
    ).fetchall())

    total_pending = conn.execute(f"SELECT COALESCE(SUM({SIGNED_AMOUNT}), 0) FROM ledger").fetchone()[0]

    return {
        "period": period,
        "sales_count": sales_count,
        "sales_total": sales_total,
        "udhaar_given": totals.get("udhaar", 0),
        "payments_received": totals.get("payment", 0),
        "total_pending": total_pending,
    }
