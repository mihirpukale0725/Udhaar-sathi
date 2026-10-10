"""The regex fallback must understand common phrasings without any API key."""

import pytest

from app import rules


@pytest.mark.parametrize("text, tool, args", [
    # add_udhaar: Hindi, Marathi, English, with currency noise
    ("Ramesh ko 500 udhaar diya", "add_udhaar", {"customer_name": "ramesh", "amount": 500}),
    ("Ramesh ji ko ₹1,500 ka udhaar diya", "add_udhaar", {"customer_name": "ramesh ji", "amount": 1500}),
    ("maine Sunita ko 200 rupaye udhar diye", "add_udhaar", {"customer_name": "sunita", "amount": 200}),
    ("Suresh ne 300 udhaar liya", "add_udhaar", {"customer_name": "suresh", "amount": 300}),
    ("Ramesh la 400 udhari dili", "add_udhaar", {"customer_name": "ramesh", "amount": 400}),
    ("500 udhaar Ramesh ko", "add_udhaar", {"customer_name": "ramesh", "amount": 500}),
    ("give 250 credit to Ramesh Patil", "add_udhaar", {"customer_name": "ramesh patil", "amount": 250}),

    # record_payment
    ("Ramesh ne 500 diye", "record_payment", {"customer_name": "ramesh", "amount": 500}),
    ("Sunita ne Rs 200 wapas kiye", "record_payment", {"customer_name": "sunita", "amount": 200}),
    ("aaj Ramesh se 500 aaye", "record_payment", {"customer_name": "ramesh", "amount": 500}),
    ("Ramesh kadun 300 aale", "record_payment", {"customer_name": "ramesh", "amount": 300}),
    ("Ramesh ne 300 dile", "record_payment", {"customer_name": "ramesh", "amount": 300}),
    ("Ramesh paid 700", "record_payment", {"customer_name": "ramesh", "amount": 700}),
    ("received 100 from Kavita", "record_payment", {"customer_name": "kavita", "amount": 100}),

    # send_reminder vs remind_all
    ("Ramesh ko reminder bhejo", "send_reminder", {"customer_name": "ramesh"}),
    ("Ramesh la reminder pathva", "send_reminder", {"customer_name": "ramesh"}),
    ("send reminder to Kavita", "send_reminder", {"customer_name": "kavita"}),
    ("sabko reminder bhejo", "remind_all", {}),
    ("sagalyanna reminder pathva", "remind_all", {}),
    ("remind everyone", "remind_all", {}),

    # get_balance
    ("Ramesh ka kitna baaki hai?", "get_balance", {"customer_name": "ramesh"}),
    ("Ramesh che kiti baki ahe", "get_balance", {"customer_name": "ramesh"}),
    ("how much does Kavita owe", "get_balance", {"customer_name": "kavita"}),

    # business_summary with period
    ("aaj kitna aaya?", "business_summary", {"period": "today"}),
    ("aaj kiti aale", "business_summary", {"period": "today"}),
    ("is hafte ki kamai", "business_summary", {"period": "week"}),
    ("is mahine ka hisaab", "business_summary", {"period": "month"}),
    ("today's sales", "business_summary", {"period": "today"}),

    # list_pending
    ("pending list dikhao", "list_pending", {}),
    ("kaun kaun baaki hai", "list_pending", {}),
    ("who owes me money", "list_pending", {}),
])
def test_parse(text, tool, args):
    decision = rules.parse(text)
    assert decision is not None, text
    assert (decision.tool, decision.args) == (tool, args)


@pytest.mark.parametrize("text", ["namaste", "mausam kaisa hai", "Ramesh ko udhaar diya"])
def test_unrecognized_returns_none(text):
    # The last one has no amount: better to say "samajh nahi aaya" than guess.
    assert rules.parse(text) is None


def test_normalize():
    assert rules.normalize("Ramesh ko ₹1,500 udhaar diya!") == "ramesh ko 1500 udhaar diya"
