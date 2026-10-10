from contextlib import closing

import pytest

from app.db import get_connection, init_db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Every test gets its own empty SQLite file, so tests never touch udhaar.db."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    # Tests must never call the real Gemini API (even if .env has a key).
    # Agent tests that need Gemini replace ask_gemini with a fake.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("WEBHOOK_SECRET", "test-secret")
    return db_path


@pytest.fixture
def shop_db():
    """A small shop with 3 customers (ids 1, 2, 3) and an empty ledger."""
    init_db()
    with closing(get_connection()) as conn:
        conn.executemany(
            "INSERT INTO customers (name, phone) VALUES (?, ?)",
            [
                ("Ramesh Patil", "+919800000001"),
                ("Sunita Deshmukh", "+919800000002"),
                ("Suresh Gupta", "+919800000003"),
            ],
        )
        conn.commit()
        yield conn
