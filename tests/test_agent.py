"""Agent tests. Gemini is never really called: tests that need it replace
agent.ask_gemini with a fake, so we can test how our code handles both good
and bad LLM output."""

from types import SimpleNamespace

import pytest

from app import agent, khata
from app.models import Decision


def run(conn, text):
    return agent.run_command(conn, text, "hi-IN")


@pytest.fixture
def fake_gemini(monkeypatch):
    """Pretend an API key is set and make Gemini return whatever the test wants."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def install(result):
        def fake(text, lang):
            if isinstance(result, Exception):
                raise result
            return result
        monkeypatch.setattr(agent, "ask_gemini", fake)

    return install


# ---------------------------------------------------- rules brain (no key) ---

def test_no_api_key_uses_rules(shop_db):
    out = run(shop_db, "Ramesh ko 500 udhaar diya")
    assert out["brain"] == "rules"
    assert out["tool_called"] == "add_udhaar"
    assert out["args"] == {"customer_name": "ramesh", "amount": 500, "note": ""}
    assert out["reply"] == "Ramesh Patil ko ₹500 udhaar likh diya. Kul baaki ₹500."
    assert khata.get_balance(shop_db, 1) == 500


def test_full_conversation(shop_db):
    run(shop_db, "Ramesh ko 500 udhaar diya")
    run(shop_db, "Suresh ko 900 udhaar diya")

    assert run(shop_db, "Ramesh ka kitna baaki hai")["reply"] == "Ramesh Patil ka ₹500 baaki hai, 0 din se."
    assert "2 logon ka kul ₹1400 baaki hai" in run(shop_db, "kaun kaun baaki hai")["reply"]
    assert run(shop_db, "Ramesh ne 200 diye")["reply"] == "Ramesh Patil se ₹200 mil gaye. Ab ₹300 baaki hai."
    assert "Khata clear" in run(shop_db, "Ramesh ne 300 diye")["reply"]


def test_not_understood(shop_db):
    out = run(shop_db, "mausam kaisa hai")
    assert out["tool_called"] is None
    assert "samajh nahi aaya" in out["reply"]


# ------------------------------------------------------- error replies ---

def test_unknown_customer_reply_suggests(shop_db):
    out = run(shop_db, "Rajesh ko 500 udhaar diya")
    assert out["reply"] == "'Rajesh' naam ka customer nahi mila. Kya aapka matlab Ramesh Patil tha?"
    assert khata.get_balance(shop_db, 1) == 0


def test_overpayment_reply(shop_db):
    run(shop_db, "Ramesh ko 500 udhaar diya")
    assert run(shop_db, "Ramesh ne 900 diye")["reply"] == "Ramesh Patil ka sirf ₹500 baaki hai."


def test_payment_with_nothing_owed_reply(shop_db):
    assert run(shop_db, "Sunita ne 100 diye")["reply"] == "Sunita Deshmukh ka koi udhaar baaki nahi hai."


# ------------------------------------------------------------ reminders ---

def messages(conn):
    return conn.execute("SELECT customer_id, direction, body FROM messages ORDER BY id").fetchall()


def test_send_reminder_writes_mock_whatsapp_message(shop_db):
    run(shop_db, "Ramesh ko 500 udhaar diya")
    out = run(shop_db, "Ramesh ko reminder bhejo")
    assert out["reply"] == "Ramesh Patil ko ₹500 ka reminder payment link ke saath bhej diya."

    [msg] = messages(shop_db)
    assert msg["customer_id"] == 1 and msg["direction"] == "out"
    assert "₹500" in msg["body"] and "/pay/" in msg["body"]


def test_no_reminder_when_nothing_owed(shop_db):
    out = run(shop_db, "Sunita ko reminder bhejo")
    assert "koi udhaar baaki nahi" in out["reply"]
    assert messages(shop_db) == []


def test_remind_all_only_reminds_people_who_owe(shop_db):
    run(shop_db, "Ramesh ko 500 udhaar diya")
    run(shop_db, "Suresh ko 200 udhaar diya")
    out = run(shop_db, "sabko reminder bhejo")
    assert out["reply"] == "2 customers ko reminder bhej diya."
    assert {m["customer_id"] for m in messages(shop_db)} == {1, 3}


# ------------------------------------------------------ business summary ---

def test_business_summary_today(shop_db):
    shop_db.execute("INSERT INTO sales (amount, created_at) VALUES (100, datetime('now', 'localtime'))")
    shop_db.execute("INSERT INTO sales (amount, created_at) VALUES (250, datetime('now', 'localtime'))")
    shop_db.execute("INSERT INTO sales (amount, created_at) VALUES (999, datetime('now', 'localtime', '-3 days'))")
    shop_db.commit()
    run(shop_db, "Ramesh ko 500 udhaar diya")
    run(shop_db, "Ramesh ne 200 diye")

    out = run(shop_db, "aaj kitna aaya")
    assert out["reply"] == "Aaj ₹350 ki UPI sale hui (2 bills). ₹200 udhaar wapas aaya, ₹500 naya udhaar diya."
    assert "₹1349" in run(shop_db, "is hafte ki kamai")["reply"]


# ---------------------------------------------- gemini brain (faked) ---

def test_gemini_decision_is_executed(shop_db, fake_gemini):
    # Gemini handled Devanagari and number words; our code just executes.
    fake_gemini(Decision(tool="add_udhaar", args={"customer_name": "Ramesh", "amount": 500}))
    out = run(shop_db, "रमेश को पाँच सौ उधार दिया")
    assert out["brain"] == "gemini"
    assert out["tool_called"] == "add_udhaar"
    assert khata.get_balance(shop_db, 1) == 500


@pytest.mark.parametrize("bad_args, expected", [
    ({"customer_name": "Ramesh", "amount": -500}, "Amount samajh nahi aaya"),
    ({"customer_name": "Ramesh", "amount": 99_99_999}, "Amount samajh nahi aaya"),
    ({"customer_name": "Ramesh", "amount": "bahut saara"}, "Amount samajh nahi aaya"),
    ({"customer_name": "Ramesh"}, "Amount samajh nahi aaya"),
    ({"customer_name": "", "amount": 500}, "naam samajh nahi aaya"),
])
def test_invalid_gemini_args_are_rejected_safely(shop_db, fake_gemini, bad_args, expected):
    fake_gemini(Decision(tool="add_udhaar", args=bad_args))
    out = run(shop_db, "anything")
    assert expected in out["reply"]
    assert shop_db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0


def test_gemini_float_amount_is_accepted_if_whole(shop_db, fake_gemini):
    # LLMs often send numbers as 500.0; that's still a whole number of rupees.
    fake_gemini(Decision(tool="add_udhaar", args={"customer_name": "Ramesh", "amount": 500.0}))
    assert run(shop_db, "x")["args"]["amount"] == 500


def test_hallucinated_tool_is_refused(shop_db, fake_gemini):
    fake_gemini(Decision(tool="delete_all_customers", args={}))
    out = run(shop_db, "sab delete karo")
    assert out["reply"] == "Yeh kaam mujhe abhi nahi aata."
    assert shop_db.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 3


def test_gemini_text_reply_is_passed_through(shop_db, fake_gemini):
    fake_gemini(Decision(reply="Kitne rupaye ka udhaar?"))
    out = run(shop_db, "Ramesh ko udhaar diya")
    assert out == {"reply": "Kitne rupaye ka udhaar?", "tool_called": None, "args": {}, "brain": "gemini"}


def test_gemini_error_falls_back_to_rules(shop_db, fake_gemini):
    fake_gemini(TimeoutError("network down"))
    out = run(shop_db, "Ramesh ko 500 udhaar diya")
    assert out["brain"] == "rules"
    assert khata.get_balance(shop_db, 1) == 500


def test_gemini_empty_response_falls_back_to_rules(shop_db, fake_gemini):
    fake_gemini(None)
    assert run(shop_db, "Ramesh ko 500 udhaar diya")["brain"] == "rules"


# --------------------------------------------- gemini response parsing ---

def test_decision_from_function_call_response():
    call = SimpleNamespace(name="get_balance", args={"customer_name": "Ramesh"})
    response = SimpleNamespace(function_calls=[call], text=None)
    assert agent.decision_from_response(response) == Decision(tool="get_balance", args={"customer_name": "Ramesh"})


def test_decision_from_text_response():
    response = SimpleNamespace(function_calls=None, text=" Kitne rupaye? ")
    assert agent.decision_from_response(response) == Decision(reply="Kitne rupaye?")


def test_every_tool_has_a_valid_gemini_declaration():
    names = [d.name for d in agent.gemini_tool_declarations()]
    assert names == list(agent.TOOLS)
    assert len(names) == 7


def test_spoken_hindi_works_without_gemini(shop_db):
    """Regression: Chrome sends Devanagari for hi-IN voice input. Without a
    Gemini key this used to reply 'samajh nahi aaya'."""
    out = run(shop_db, "रमेश को 500 उधार दिया")
    assert out["brain"] == "rules"
    assert out["reply"] == "Ramesh Patil ko ₹500 udhaar likh diya. Kul baaki ₹500."
    assert run(shop_db, "रमेश ने 200 दिए")["reply"] == "Ramesh Patil se ₹200 mil gaye. Ab ₹300 baaki hai."
