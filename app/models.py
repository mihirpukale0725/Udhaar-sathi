"""Pydantic models: the shape of every request and response.

Request models validate input at the HTTP boundary (FastAPI returns 422 if
they fail). khata.py validates again, because the AI agent calls khata
directly without going through HTTP.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.khata import MAX_AMOUNT


# --------------------------------------------------------------- requests ---

class EntryRequest(BaseModel):
    # Strip spaces so "   " fails min_length instead of becoming a blank name.
    model_config = ConfigDict(str_strip_whitespace=True)

    customer_name: str = Field(min_length=1, max_length=100, examples=["Ramesh"])
    # int (not float): 500.5 is rejected. gt/le give clear 422 errors.
    amount: int = Field(gt=0, le=MAX_AMOUNT, examples=[500])
    note: str | None = Field(default=None, max_length=200, examples=["atta, dal"])


class UdhaarRequest(EntryRequest):
    """Owner gave goods on credit."""


class PaymentRequest(EntryRequest):
    """Customer paid back (cash or UPI)."""


# -------------------------------------------------------------- responses ---

class LedgerEntry(BaseModel):
    id: int
    type: Literal["udhaar", "payment"]
    amount: int
    note: str | None
    created_at: str
    balance_after: int  # running balance right after this entry (computed, not stored)


class CustomerSummary(BaseModel):
    id: int
    name: str
    phone: str | None
    balance: int
    days_pending: int | None  # age of the oldest unpaid udhaar; None if nothing is due


class CustomerDetail(CustomerSummary):
    ledger: list[LedgerEntry]


class TransactionResponse(BaseModel):
    message: str  # human-friendly sentence; the UI will speak this aloud later
    customer: CustomerSummary
    entry: LedgerEntry


class KhataCustomer(CustomerSummary):
    trust_score: int  # 0-100, see insights.py


class KhataResponse(BaseModel):
    total_pending: int
    customers: list[KhataCustomer]


class CommandRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=500, examples=["Ramesh ko 500 udhaar diya"])
    # Browser speech-recognition language. Helps Gemini; the rules fallback ignores it.
    lang: Literal["hi-IN", "mr-IN", "en-IN"] = "hi-IN"


class CommandResponse(BaseModel):
    reply: str                                # short Hinglish, spoken aloud by the UI
    tool_called: str | None                   # None if nothing was understood
    args: dict                                # validated arguments the tool ran with
    brain: Literal["gemini", "rules"]         # which parser understood the command


# ------------------------------------------------------------ agent tools ---
# Each model below does two jobs:
#   1. model_json_schema() becomes the tool's parameter schema sent to Gemini.
#   2. model_validate() checks the arguments Gemini sends back.
# One definition, so the schema Gemini sees and the checks we run can't drift apart.
# Field descriptions are written for the LLM: they are part of the prompt.

class CustomerArgs(BaseModel):
    customer_name: str = Field(
        min_length=1, max_length=100,
        description="Customer name in English letters, without ji/bhai/tai. E.g. 'Ramesh'.",
    )


class CustomerAmountArgs(CustomerArgs):
    amount: int = Field(gt=0, le=MAX_AMOUNT, description="Amount in whole rupees, e.g. 500.")
    # str with "" default (not str | None): keeps the JSON schema simple for Gemini.
    note: str = Field(default="", max_length=200, description="Optional: items bought, e.g. 'atta, dal'.")


class NoArgs(BaseModel):
    pass


class SummaryArgs(BaseModel):
    period: Literal["today", "week", "month"] = Field(
        default="today", description="'today' for aaj, 'week' for is hafte, 'month' for is mahine.",
    )


class Decision(BaseModel):
    """What a 'brain' (Gemini or rules) decided to do. Not yet validated or executed."""

    tool: str | None = None   # None = no tool; just say `reply`
    args: dict = Field(default_factory=dict)
    reply: str | None = None  # Gemini's own text, used only when it calls no tool


# -------------------------------------------------------------- payments ---

class CheckoutRequest(BaseModel):
    method: Literal["paytm", "gpay", "phonepe"]


class CheckoutResponse(BaseModel):
    """What the fake gateway hands back: the exact webhook body and its signature."""

    payload: str     # JSON text; must be sent byte-for-byte, or the signature won't match
    signature: str   # hex HMAC-SHA256 of payload


class WebhookPayload(BaseModel):
    event: Literal["payment.captured"]
    payment_id: str = Field(pattern=r"^pay_[A-Za-z0-9]{8,40}$")
    token: str = Field(min_length=8, max_length=64)
    amount: int = Field(gt=0)
    method: str = Field(max_length=20)


class WebhookResponse(BaseModel):
    status: Literal["processed", "already_processed"]
    payment_id: str
    balance: int | None = None  # customer's balance after this payment


class Message(BaseModel):
    id: int
    customer_id: int
    customer_name: str
    direction: Literal["out", "in"]
    kind: Literal["reminder", "thank_you", "other"]
    body: str
    created_at: str


# ------------------------------------------------------------- dashboard ---

class BusinessSummary(BaseModel):
    period: Literal["today", "week", "month"]
    sales_count: int
    sales_total: int
    udhaar_given: int
    payments_received: int
    total_pending: int


class ActivityItem(BaseModel):
    id: int
    customer_id: int
    customer_name: str
    type: Literal["udhaar", "payment"]
    amount: int
    note: str | None
    created_at: str


class ReminderResponse(BaseModel):
    reply: str        # short Hinglish, shown/spoken in the UI
    link: str         # the payment link that was sent
    firm: bool        # True if the firmer (15+ days) wording was used


# -------------------------------------------------------------- insights ---

class WeekOverWeek(BaseModel):
    this_week: int                # sales in the last 7 days
    last_week: int                # sales in the 7 days before that
    change_pct: float | None      # None when last week had no sales (can't divide by 0)


class DailySales(BaseModel):
    date: str
    total: int
    bills: int


class StuckUdhaar(BaseModel):
    id: int
    name: str
    phone: str | None
    balance: int
    days_pending: int


class MissingRegular(BaseModel):
    id: int
    name: str
    last_seen: str
    days_since: int


class TrustedCustomer(BaseModel):
    id: int
    name: str
    trust_score: int


class PeakTimes(BaseModel):
    day: str | None          # e.g. "Sunday"
    hour: int | None         # 0-23
    hour_label: str | None   # e.g. "7 PM - 8 PM"


class InsightsResponse(BaseModel):
    sales_week: WeekOverWeek
    daily_sales: list[DailySales]
    stuck_udhaar: list[StuckUdhaar]
    regulars_missing: list[MissingRegular]
    trusted_customers: list[TrustedCustomer]
    peak: PeakTimes


class ErrorResponse(BaseModel):
    """Shape of every error we return. Extra fields like suggestions may be present."""

    model_config = ConfigDict(extra="allow")

    error: str
    detail: str
