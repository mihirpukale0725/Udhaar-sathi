"""Fill the database with realistic demo data.

Run with:  python -m app.seed
(It wipes existing data first, so it is safe to re-run.)

What it creates:
- 8 customers, each with a repayment "habit" that drives their ledger history.
  The trust score (a later step) should be able to tell these habits apart.
- 30 days (plus today so far) of fake UPI sales with realistic hourly patterns:
  morning and evening rush, busier weekends, and Sunday evenings busiest.

A fixed random seed makes the data identical on every run, so tests and
demos are reproducible.
"""

import random
from contextlib import closing
from datetime import datetime, timedelta

from app.db import get_connection, init_db

# Phone numbers are deliberately fake (98000000xx) - never seed real numbers.
# habit -> how many days a customer takes to pay back each udhaar
#   punctual: 1-5 days     regular: 7-14 days     slow: 15-25 days
#   overdue:  used to pay (slowly), but stopped. 'overdue_days' is the age of
#             their oldest unpaid udhaar; nothing since then has been paid.
CUSTOMERS = [
    {"name": "Ramesh Patil",    "phone": "+919800000001", "habit": "punctual"},
    {"name": "Sunita Deshmukh", "phone": "+919800000002", "habit": "punctual"},
    {"name": "Anil Sharma",     "phone": "+919800000003", "habit": "punctual"},
    {"name": "Priya Kulkarni",  "phone": "+919800000004", "habit": "regular"},
    {"name": "Imran Shaikh",    "phone": "+919800000005", "habit": "slow"},
    {"name": "Ganesh Jadhav",   "phone": "+919800000006", "habit": "overdue", "overdue_days": 22},
    {"name": "Kavita Yadav",    "phone": "+919800000007", "habit": "overdue", "overdue_days": 28},
    {"name": "Suresh Gupta",    "phone": "+919800000008", "habit": "overdue", "overdue_days": 35},
]

PAY_DELAY_DAYS = {
    "punctual": (1, 5),
    "regular": (7, 14),
    "slow": (15, 25),
    "overdue": (15, 25),  # how they paid *before* they stopped paying
}

ITEMS = ["atta, dal", "rice 5kg", "oil, sugar", "milk, bread", "masala, tea",
         "soap, detergent", "biscuits, namkeen", "grocery"]

# How many reminders each habit needed in the past (feeds the trust score).
REMINDERS_SENT = {"punctual": (0, 0), "regular": (1, 1), "slow": (2, 3), "overdue": (3, 5)}

HISTORY_DAYS = 60   # how far back the udhaar history goes
SALES_DAYS = 30


def _at(day: datetime, hour: int, minute: int) -> str:
    """Format a timestamp the same way SQLite's datetime() does."""
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def build_ledger(customer: dict, now: datetime, rng: random.Random) -> list[tuple]:
    """Return (type, amount, note, created_at) rows for one customer."""
    habit = customer["habit"]
    overdue_days = customer.get("overdue_days")

    # Pick 4-7 random days in the past on which the customer took udhaar.
    purchase_days = set(rng.sample(range(1, HISTORY_DAYS + 1), rng.randint(4, 7)))
    if overdue_days:
        # Guarantee the oldest unpaid udhaar is exactly 'overdue_days' old,
        # and that they have some older (paid) history before that.
        purchase_days = {d for d in purchase_days if d != overdue_days}
        purchase_days.add(overdue_days)
        purchase_days.add(rng.randint(overdue_days + 10, HISTORY_DAYS))

    rows = []
    for days_ago in sorted(purchase_days, reverse=True):  # oldest first
        day = now - timedelta(days=days_ago)
        amount = rng.choice(range(100, 1501, 50))  # Rs 100 - 1500 in steps of 50
        rows.append(("udhaar", amount, rng.choice(ITEMS), _at(day, rng.randint(8, 21), rng.randint(0, 59))))

        # Overdue customers never paid anything from their cutoff onwards.
        if overdue_days and days_ago <= overdue_days:
            continue

        paid_days_ago = days_ago - rng.randint(*PAY_DELAY_DAYS[habit])
        if paid_days_ago < 0:
            continue  # repayment would be in the future -> still open (not overdue)

        pay_day = now - timedelta(days=paid_days_ago)
        rows.append(("payment", amount, "UPI", _at(pay_day, rng.randint(8, 21), rng.randint(0, 59))))

    # Keep rows in time order so the ledger reads naturally.
    return sorted(rows, key=lambda r: r[3])


def build_reminders(customer: dict, now: datetime, rng: random.Random) -> list[tuple]:
    """Return (body, created_at) rows for past reminders, spread over the last 60 days."""
    count = rng.randint(*REMINDERS_SENT[customer["habit"]])
    days = sorted(rng.sample(range(1, HISTORY_DAYS + 1), count), reverse=True)
    body = f"Namaste {customer['name']} ji, aapka udhaar baaki hai. Kripya jaldi chuka dein."
    return [(body, _at(now - timedelta(days=d), rng.randint(9, 20), rng.randint(0, 59))) for d in days]


# Relative footfall per hour of the day (store open 7 AM - 10 PM).
# Two peaks: a morning rush (milk, bread) and a bigger evening rush (dinner groceries).
HOURLY_WEIGHTS = {
    7: 6, 8: 8, 9: 7, 10: 5, 11: 4, 12: 4, 13: 3, 14: 2,
    15: 3, 16: 4, 17: 6, 18: 10, 19: 13, 20: 9, 21: 4,
}


def build_sales(now: datetime, rng: random.Random) -> list[tuple]:
    """Return (amount, created_at) rows for the last SALES_DAYS full days, plus
    today up to the current time (so "aaj kitna aaya?" has an answer in demos)."""
    rows = []
    for days_ago in range(0, SALES_DAYS + 1):
        day = now - timedelta(days=days_ago)
        weekday = day.weekday()  # Monday=0 ... Sunday=6

        weights = dict(HOURLY_WEIGHTS)
        if weekday == 5:          # Saturday: busier all day
            num_sales = rng.randint(50, 60)
        elif weekday == 6:        # Sunday: busiest, especially the evening
            num_sales = rng.randint(60, 75)
            for hour in range(17, 22):
                weights[hour] *= 2
        else:                     # weekdays
            num_sales = rng.randint(35, 45)

        hours = rng.choices(list(weights), weights=list(weights.values()), k=num_sales)
        for hour in hours:
            # Log-normal gives many small bills and a few big ones (median ~Rs 90).
            amount = max(10, min(2000, round(rng.lognormvariate(4.5, 0.7))))
            created_at = _at(day, hour, rng.randint(0, 59))
            if created_at <= now.strftime("%Y-%m-%d %H:%M:%S"):  # no sales from the future
                rows.append((amount, created_at))

    return sorted(rows, key=lambda r: r[1])


def seed(db_path: str | None = None, rng_seed: int = 42) -> None:
    """Wipe all data and insert fresh demo data."""
    init_db(db_path)
    rng = random.Random(rng_seed)
    now = datetime.now()

    with closing(get_connection(db_path)) as conn:
        # 'with conn' = one transaction: either everything is inserted or nothing.
        with conn:
            # Children first, so foreign keys aren't violated.
            for table in ("ledger", "payment_links", "messages", "sales", "customers"):
                conn.execute(f"DELETE FROM {table}")
            # Reset AUTOINCREMENT counters so ids start at 1 again.
            conn.execute("DELETE FROM sqlite_sequence")

            for customer in CUSTOMERS:
                cur = conn.execute(
                    "INSERT INTO customers (name, phone) VALUES (?, ?)",
                    (customer["name"], customer["phone"]),
                )
                customer_id = cur.lastrowid
                conn.executemany(
                    "INSERT INTO ledger (customer_id, type, amount, note, created_at) VALUES (?, ?, ?, ?, ?)",
                    [(customer_id, *row) for row in build_ledger(customer, now, rng)],
                )
                conn.executemany(
                    "INSERT INTO messages (customer_id, direction, kind, body, created_at) "
                    "VALUES (?, 'out', 'reminder', ?, ?)",
                    [(customer_id, *row) for row in build_reminders(customer, now, rng)],
                )

            conn.executemany(
                "INSERT INTO sales (amount, created_at) VALUES (?, ?)",
                build_sales(now, rng),
            )


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    seed()

    # Print a quick summary so you can see the seed worked.
    with closing(get_connection()) as conn:
        print("Customer balances (computed from ledger):")
        for row in conn.execute(
            """
            SELECT c.name,
                   COALESCE(SUM(CASE l.type WHEN 'udhaar' THEN l.amount ELSE -l.amount END), 0) AS balance
            FROM customers c LEFT JOIN ledger l ON l.customer_id = c.id
            GROUP BY c.id ORDER BY c.id
            """
        ):
            print(f"  {row['name']:<16} Rs {row['balance']}")
        n_sales = conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
        print(f"Seeded {n_sales} UPI sales over the last {SALES_DAYS} days + today so far.")
