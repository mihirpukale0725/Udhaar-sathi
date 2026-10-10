"""Devanagari -> Latin and number words -> digits (app/hindi.py).

Chrome's hi-IN / mr-IN speech recognition returns Devanagari, so without
these the offline rules parser answered "samajh nahi aaya" to spoken Hindi.
"""

import pytest

from app import rules
from app.hindi import to_latin, words_to_digits


@pytest.mark.parametrize("devanagari, latin", [
    ("रमेश", "ramesh"),        # final hidden "a" dropped
    ("को", "ko"),
    ("उधार", "udhaar"),
    ("दिया", "diya"),          # final "aa" -> "a"
    ("सबको", "sabko"),         # middle hidden "a" dropped (not "sabako")
    ("कितना", "kitna"),
    ("इमरान", "imraan"),
    ("प्रिया", "priya"),       # halant joins प and र
    ("हज़ार", "hazaar"),        # nukta: ज़ = z
    ("पाँच", "paanch"),         # chandrabindu
    ("५००", "500"),            # Devanagari digits
    ("दिया।", "diya"),         # danda (Hindi full stop) is punctuation
])
def test_to_latin(devanagari, latin):
    assert to_latin(devanagari).strip() == latin


def test_to_latin_leaves_latin_text_alone():
    assert to_latin("Ramesh ko 500 udhaar diya") == "Ramesh ko 500 udhaar diya"


@pytest.mark.parametrize("words, digits", [
    ("paanch sau", "500"),
    ("dedh hazaar", "1500"),
    ("dhai sau", "250"),
    ("do hazaar paanch sau", "2500"),
    ("sau", "100"),
    ("pachaas", "50"),
    ("5 sau", "500"),
])
def test_words_to_digits(words, digits):
    assert words_to_digits(words) == digits


@pytest.mark.parametrize("text", [
    "ramesh ko reminder bhej do",   # "do" = "give", not 2
    "ramesh ko 500 udhaar diya",    # plain digits unchanged
    "das minute mein aana",         # "das" alone is ambiguous: left alone
])
def test_words_to_digits_leaves_ambiguous_words(text):
    assert words_to_digits(text) == text


@pytest.mark.parametrize("spoken, tool, args", [
    ("रमेश को 500 उधार दिया", "add_udhaar", {"customer_name": "ramesh", "amount": 500}),
    ("रमेश को ५०० रुपये उधार दिए", "add_udhaar", {"customer_name": "ramesh", "amount": 500}),
    ("रमेश को पाँच सौ का उधार दिया", "add_udhaar", {"customer_name": "ramesh", "amount": 500}),
    ("सुनीता को डेढ़ हज़ार उधार दिया", "add_udhaar", {"customer_name": "sunita", "amount": 1500}),
    ("रमेश ला 500 उधारी दिली", "add_udhaar", {"customer_name": "ramesh", "amount": 500}),
    ("रमेश ने 200 दिए", "record_payment", {"customer_name": "ramesh", "amount": 200}),
    ("रमेश ने 200 रुपए वापस किए", "record_payment", {"customer_name": "ramesh", "amount": 200}),
    ("कविता का कितना बाकी है", "get_balance", {"customer_name": "kavita"}),
    ("गणेश को रिमाइंडर भेजो", "send_reminder", {"customer_name": "ganesh"}),
    ("सबको रिमाइंडर भेजो", "remind_all", {}),
    ("आज कितना आया", "business_summary", {"period": "today"}),
    ("कौन कौन बाकी है", "list_pending", {}),
    ("Ramesh ko paanch sau udhaar diya", "add_udhaar", {"customer_name": "ramesh", "amount": 500}),
    ("Ramesh ko 500 ka saman udhaar diya", "add_udhaar", {"customer_name": "ramesh", "amount": 500}),
])
def test_rules_understand_spoken_hindi(spoken, tool, args):
    decision = rules.parse(spoken)
    assert decision is not None, spoken
    assert (decision.tool, decision.args) == (tool, args)
