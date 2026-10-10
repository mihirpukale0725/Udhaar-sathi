"""Regex fallback "brain" for common Hinglish / Marathi / English commands.

Used when there is no GEMINI_API_KEY or the Gemini call fails, so the core
features still work offline. Input is first converted to romanized text with
digits by app/hindi.py, so all of these work:
    "Ramesh ko 500 udhaar diya"
    "रमेश को 500 उधार दिया"             (what Chrome sends for spoken Hindi)
    "Ramesh ko paanch sau udhaar diya"

parse() returns the same Decision object as the Gemini path, so the agent
validates and executes both in exactly the same way.
"""

import re

from app.hindi import to_latin, words_to_digits
from app.models import Decision

# Words people say that are never part of a name ("maine Ramesh ko...").
FILLER_WORDS = {"aaj", "abhi", "kal", "maine", "mai", "main", "humne", "please", "plz", "bhaiya", "mi", "aata"}

# Currency words are removed before matching so "Rs 500", "500 rupaye" and "₹500" all become "500".
CURRENCY = r"(?:₹|rs\.?|rupaye|rupay|rupaiye|rupiye|rupees|rupee|rupya|rupye|rupae)"

UDHAAR = r"(?:udhaar|udhar|udhaari|udhari|credit)"
TIME = r"(?:aaj|today|hafte|hafta|week|mahine|mahina|month)"
REMINDER = r"(?:reminder|remind|yaad|rim\w*nd\w*r)"                       # रिमाइंडर -> "rimaindar"
EVERYONE = r"(?:sab|sabko|sabhi|sabhiko|sabhi ko|saga?ly\w*|everyone|all)"  # सगळ्यांना -> "sagalyaanna"
FILLER = r"(?:\S+ ){0,3}?"  # up to 3 extra words: "500 ka saman udhaar", "500 rupaye wapas diye"
PAID_VERB = (r"(?:diye|diya|die|de diye|de diya|dile|dila|kiye|kiya|kie|chukaye|chukaya|chukae|"
             r"bhare|bhara|mile|mila|aaye|aaya|aae|aale|lautaye|lautaya|lautae|paid)")

# Ordered list of (tool, pattern). The FIRST match wins, so more specific
# patterns come first: "sabko reminder" before "X ko reminder", and patterns
# that need an amount before the summary ("aaj Ramesh se 500 aaye" is a payment).
PATTERNS: list[tuple[str, str]] = [
    # --- remind_all: "sabko reminder bhejo", "sagalyanna reminder pathva", "remind everyone"
    ("remind_all", rf"\b{EVERYONE}\b.*\b{REMINDER}"),
    ("remind_all", rf"\b{REMINDER}\b.*\b{EVERYONE}\b"),

    # --- send_reminder: "Ramesh ko reminder bhejo", "Ramesh la reminder pathva", "remind Ramesh"
    ("send_reminder", rf"^(?P<name>.+?) (?:ko|la|na) {REMINDER}"),
    ("send_reminder", r"\b(?:send )?reminder to (?P<name>.+)$"),
    ("send_reminder", r"^remind (?P<name>.+)$"),

    # --- add_udhaar: "Ramesh ko 500 udhaar diya", "Ramesh ne 500 udhaar liya", "Ramesh la 500 udhari dili"
    ("add_udhaar", rf"^(?P<name>.+?) (?:ko|ne|la|na|ke naam) (?P<amount>\d+) {FILLER}{UDHAAR}"),
    ("add_udhaar", rf"^(?P<amount>\d+) (?:ka |che |chi )?{UDHAAR} (?P<name>.+?) (?:ko|la|na|ke naam)\b"),
    ("add_udhaar", rf"\b(?:give|gave|add|added) (?P<amount>\d+) (?:of )?{UDHAAR} to (?P<name>.+)$"),

    # --- record_payment: "Ramesh ne 500 diye", "Ramesh ne 500 wapas kiye", "Ramesh kadun 500 aale"
    ("record_payment", rf"^(?P<name>.+?) (?:ne|kadun|kadoon|se) (?P<amount>\d+) {FILLER}{PAID_VERB}\b"),
    ("record_payment", r"^(?P<name>.+?) (?:has )?paid (?P<amount>\d+)"),
    ("record_payment", r"\breceived (?P<amount>\d+) from (?P<name>.+)$"),

    # --- get_balance: "Ramesh ka kitna baaki hai", "Ramesh che kiti baki", "how much does Ramesh owe"
    # The (?!...) lookahead skips sentences about a time period ("is mahine ka hisaab").
    ("get_balance", rf"^(?!.*\b{TIME}\b)(?P<name>.+?) (?:ka|ki|ke|che|chi|cha|cho) (?:kitna|kitne|kiti|hisaab|hisab|balance|baaki|baki|khata)\b"),
    ("get_balance", r"\bhow much (?:does |is )?(?P<name>.+?) (?:owe|owes|pending)\b"),
    ("get_balance", r"\bbalance (?:of|for) (?P<name>.+)$"),

    # --- business_summary: "aaj kitna aaya", "aaj kiti aale", "is hafte ki kamai", "today's sales"
    ("business_summary", rf"\b{TIME}\b.*\b(?:kitna|kitne|kiti|kamaa?i|aaya|aaye|aae|aale|sale|sales|bikri|galla|summary|hisaab|hisab)\b"),
    ("business_summary", r"\b(?:kamaa?i|bikri|galla|sales summary|business summary)\b"),

    # --- list_pending: "pending list", "kaun kaun baaki hai", "kisne paise dene hai", "who owes"
    ("list_pending", r"\b(?:pending|baaki|baki|udhaar|udhar) (?:list|yaadi|yadi|dikhao|batao)\b"),
    ("list_pending", r"\b(?:kaun kaun|kis kis|kisne|kiske|kon kon)\b"),
    ("list_pending", r"\b(?:who owes|khata dikhao|sab ka hisaab|sabka hisaab)\b"),
]

PERIOD_WORDS = [
    ("week", r"\b(?:hafte|hafta|week|saptah|aathavda)\b"),
    ("month", r"\b(?:mahine|mahina|month|mahinya)\b"),
]


def normalize(text: str) -> str:
    """'Ramesh ko ₹1,500 udhaar diya!'  -> 'ramesh ko 1500 udhaar diya'
    'रमेश को पाँच सौ उधार दिया'      -> 'ramesh ko 500 udhaar diya'"""
    text = to_latin(text).lower()                              # Devanagari -> Latin letters
    text = re.sub(r"(?<=\d),(?=\d)", "", text)                 # 1,500 -> 1500
    text = re.sub(rf"{CURRENCY}\s*(?=\d)|(?<=\d)\s*{CURRENCY}\b", " ", text)  # drop Rs/₹/rupaye next to numbers
    text = re.sub(r"[^\w\s']", " ", text)                     # drop punctuation
    text = text.replace("'s", "")
    text = re.sub(r"\s+", " ", text).strip()
    return words_to_digits(text)                               # "paanch sau" -> "500"


def clean_name(raw: str) -> str:
    """Remove filler words around the name: 'maine ramesh' -> 'ramesh'."""
    return " ".join(w for w in raw.split() if w not in FILLER_WORDS)


def parse(text: str) -> Decision | None:
    """Return a Decision for the first matching pattern, or None if nothing matched."""
    norm = normalize(text)
    for tool, pattern in PATTERNS:
        match = re.search(pattern, norm)
        if not match:
            continue

        groups = match.groupdict()
        args: dict = {}
        if "name" in groups:
            args["customer_name"] = clean_name(groups["name"])
            if not args["customer_name"]:
                continue  # the "name" was only filler words; try the next pattern
        if "amount" in groups:
            args["amount"] = int(groups["amount"])
        if tool == "business_summary":
            args["period"] = next((p for p, rx in PERIOD_WORDS if re.search(rx, norm)), "today")
        return Decision(tool=tool, args=args)
    return None
