"""FastAPI entry point.

Run with:  uvicorn app.main:app --reload

Endpoints are kept thin: Pydantic validates the request, khata.py does the
work, and the exception handlers below turn errors into clear JSON.
"""

import sqlite3
from contextlib import asynccontextmanager
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import khata
from app.db import get_connection, init_db
from app.models import (
    CustomerDetail,
    ErrorResponse,
    KhataResponse,
    PaymentRequest,
    TransactionResponse,
    UdhaarRequest,
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
ERRORS = {404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 400: {"model": ErrorResponse}}


# --------------------------------------------------------------- endpoints ---

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
    """All customers with pending udhaar, biggest balance first."""
    customers = khata.list_pending(conn)
    return {"total_pending": sum(c["balance"] for c in customers), "customers": customers}


@app.get("/customer/{customer_id}", response_model=CustomerDetail, responses=ERRORS)
def get_customer(customer_id: int, conn: DB):
    """One customer with their full ledger history and running balance."""
    return khata.get_customer_detail(conn, customer_id)
