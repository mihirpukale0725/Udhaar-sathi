"""FastAPI entry point.

Run with:  uvicorn app.main:app --reload

Endpoints are kept thin: Pydantic validates the request, khata.py does the
work, and the exception handlers below turn errors into clear JSON.
"""

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from app import agent, insights, khata, payments
from app.db import get_connection, init_db
from app.models import (
    ActivityItem,
    BusinessSummary,
    CheckoutRequest,
    CheckoutResponse,
    CommandRequest,
    CommandResponse,
    CustomerDetail,
    ErrorResponse,
    InsightsResponse,
    KhataResponse,
    Message,
    PaymentRequest,
    ReminderResponse,
    TransactionResponse,
    UdhaarRequest,
    WebhookResponse,
)

# Read .env before anything else looks at environment variables.
load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs once at startup: make sure all tables exist.
    # CREATE TABLE IF NOT EXISTS makes this safe to run every time.
    init_db()
    yield


app = FastAPI(title="Udhaar Saathi", lifespan=lifespan)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


# ------------------------------------------------------------ dependencies ---

def get_db():
    """One connection per request, always closed afterwards (even on errors)."""
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


DB = Annotated[sqlite3.Connection, Depends(get_db)]


# ---------------------------------------------------------- error handling ---

# Business errors -> HTTP status codes. Kept here (not in khata.py) so the
# business layer knows nothing about HTTP.
ERROR_STATUS = {
    khata.CustomerNotFound: 404,
    khata.AmbiguousCustomer: 409,
    khata.InvalidAmount: 400,
    payments.InvalidSignature: 401,
    payments.LinkNotFound: 404,
    payments.LinkUnavailable: 409,
    payments.BadPayload: 400,
}


@app.exception_handler(khata.KhataError)
async def khata_error_handler(request: Request, exc: khata.KhataError) -> JSONResponse:
    return JSONResponse(
        status_code=ERROR_STATUS.get(type(exc), 400),
        content={"error": exc.code, "detail": exc.message, **exc.extra},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Flatten Pydantic's nested error list into one readable sentence,
    e.g. "amount: Input should be greater than 0"."""
    problems = []
    for err in exc.errors():
        field = ".".join(str(part) for part in err["loc"] if part != "body")
        problems.append(f"{field}: {err['msg']}" if field else err["msg"])
    return JSONResponse(status_code=422, content={"error": "invalid_request", "detail": "; ".join(problems)})


# Documents the error shapes in the auto-generated /docs page.
ERRORS = {code: {"model": ErrorResponse} for code in (400, 404, 409)}


# --------------------------------------------------------------- endpoints ---

@app.get("/", include_in_schema=False)
def index():
    """The single-page dashboard."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict:
    """Liveness check used by tests and for quick manual verification."""
    return {"status": "ok"}


@app.post("/udhaar", response_model=TransactionResponse, status_code=201, responses=ERRORS)
def create_udhaar(req: UdhaarRequest, conn: DB):
    """Record udhaar (credit) given to a customer. Name is fuzzy-matched."""
    return khata.add_udhaar(conn, req.customer_name, req.amount, req.note)


@app.post("/payment", response_model=TransactionResponse, status_code=201, responses=ERRORS)
def create_payment(req: PaymentRequest, conn: DB):
    """Record a payment received. Can't exceed what the customer owes."""
    return khata.add_payment(conn, req.customer_name, req.amount, req.note)


@app.get("/khata", response_model=KhataResponse)
def get_khata(conn: DB):
    """All customers with pending udhaar, biggest balance first, with trust scores."""
    customers = [{**c, "trust_score": insights.trust_score(conn, c["id"])} for c in khata.list_pending(conn)]
    return {"total_pending": sum(c["balance"] for c in customers), "customers": customers}


@app.get("/summary", response_model=BusinessSummary)
def get_summary(conn: DB, period: Literal["today", "week", "month"] = "today"):
    """Sales, udhaar given, udhaar received in the period, plus total pending."""
    return insights.business_summary(conn, period)


@app.get("/activity", response_model=list[ActivityItem])
def get_activity(conn: DB, limit: Annotated[int, Query(ge=1, le=200)] = 30):
    """Latest khata entries across all customers, newest first."""
    rows = conn.execute(
        """
        SELECT l.id, l.customer_id, c.name AS customer_name, l.type, l.amount, l.note, l.created_at
        FROM ledger l JOIN customers c ON c.id = l.customer_id
        ORDER BY l.created_at DESC, l.id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


@app.post("/customer/{customer_id}/remind", response_model=ReminderResponse, responses=ERRORS)
def remind_customer(customer_id: int, conn: DB):
    """Remind button in the khata table. By id, so it can't pick the wrong 'Ramesh'."""
    r = payments.send_reminder_to(conn, customer_id)
    c = r["customer"]
    return {"reply": f"{c['name']} ko ₹{c['balance']} ka reminder bhej diya.", "link": r["link"], "firm": r["firm"]}


@app.get("/insights", response_model=InsightsResponse)
def get_insights(conn: DB):
    """Dashboard numbers: sales trend, stuck udhaar, missing regulars, trusted customers, peak times."""
    return insights.get_insights(conn)


@app.get("/customer/{customer_id}", response_model=CustomerDetail, responses=ERRORS)
def get_customer(customer_id: int, conn: DB):
    """One customer with their full ledger history and running balance."""
    return khata.get_customer_detail(conn, customer_id)


@app.post("/command", response_model=CommandResponse)
def command(req: CommandRequest, conn: DB):
    """Voice/text command in Hindi, Marathi or English -> one tool call -> spoken reply.

    Always returns 200 with a reply: for a voice UI, "Ramesh naam ka customer
    nahi mila" is a normal answer to speak, not an HTTP failure.
    """
    return agent.run_command(conn, req.text, req.lang)


@app.get("/messages", response_model=list[Message])
def list_messages(conn: DB, customer_id: int | None = None, limit: Annotated[int, Query(ge=1, le=200)] = 50):
    """MOCK WhatsApp inbox: newest messages first, optionally for one customer."""
    rows = conn.execute(
        """
        SELECT m.id, m.customer_id, c.name AS customer_name, m.direction, m.kind, m.body, m.created_at
        FROM messages m JOIN customers c ON c.id = m.customer_id
        WHERE ? IS NULL OR m.customer_id = ?
        ORDER BY m.id DESC LIMIT ?
        """,
        (customer_id, customer_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- payments ---

@app.get("/pay/{token}", response_class=HTMLResponse)
def pay_page(token: str, conn: DB):
    """MOCK UPI payment page that the reminder link opens."""
    status, page = payments.render_pay_page(conn, token)
    return HTMLResponse(page, status_code=status)


@app.post("/gateway/checkout/{token}", response_model=CheckoutResponse, responses=ERRORS)
def gateway_checkout(token: str, req: CheckoutRequest, conn: DB):
    """MOCK payment gateway: pretends money moved and returns a signed webhook."""
    return payments.gateway_checkout(conn, token, req.method)


async def raw_body(request: Request) -> bytes:
    # The signature covers the exact bytes sent, so we must read the raw body
    # instead of letting FastAPI parse the JSON for us.
    return await request.body()


@app.post("/webhook/payment", response_model=WebhookResponse,
          responses={401: {"model": ErrorResponse}, **ERRORS})
def payment_webhook(
    body: Annotated[bytes, Depends(raw_body)],
    conn: DB,
    x_signature: Annotated[str | None, Header()] = None,  # reads the "X-Signature" header
):
    """Gateway calls this when a payment succeeds. Signed and idempotent."""
    return payments.handle_webhook(conn, body, x_signature)
