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


class KhataResponse(BaseModel):
    total_pending: int
    customers: list[CustomerSummary]


class ErrorResponse(BaseModel):
    """Shape of every error we return. Extra fields like suggestions may be present."""

    model_config = ConfigDict(extra="allow")

    error: str
    detail: str
