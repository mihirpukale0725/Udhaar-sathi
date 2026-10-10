"""The agent: turns a spoken command into one validated tool call.

Flow for every command:

    text --> Gemini (function calling) ----+
      |      (no API key, or it failed)    +--> Decision(tool, args) --> validate --> execute --> Hinglish reply
      +----> rules.py (regex fallback) ----+

Safety design:
- The LLM only *chooses* a tool and its arguments. It never touches the DB.
- Arguments are validated with the same Pydantic model that generated the
  tool's schema, then executed by our own code (khata.py / insights.py),
  which enforces the business rules again.
- Replies are built from templates using real DB results, so the spoken
  numbers can never be hallucinated. This also means one LLM call per
  command instead of two, which keeps voice latency low.
"""

import logging
import os
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable

from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError

from app import insights, khata, payments, rules
from app.models import CustomerAmountArgs, CustomerArgs, Decision, NoArgs, SummaryArgs

log = logging.getLogger(__name__)

# "gemini-flash-latest" always points at Google's current stable Flash model.
# Pin a specific version in .env (GEMINI_MODEL=...) for reproducible results.
DEFAULT_MODEL = "gemini-flash-latest"
GEMINI_TIMEOUT_MS = 10_000  # voice UX: fall back to rules rather than hang

SYSTEM_PROMPT = """You are Udhaar Saathi, the khata (credit ledger) assistant of an Indian kirana shop.
The shop owner speaks Hindi, Marathi, English or a mix, sometimes in Devanagari script.
Call exactly one tool for each command.

Rules:
- customer_name: always in English letters (रमेश -> Ramesh). Drop ji/bhai/tai/kaka.
- amount: whole rupees as a number. Convert number words: "paanch sau" = 500, "dedh hazaar" = 1500.
- "X ko N udhaar diya" or "X ne N udhaar liya" means add_udhaar (the shop gave credit).
- "X ne N diye / wapas kiye / jama kiye" or "X kadun N aale" means record_payment (the customer paid).
- Never guess a name or amount that was not said. If something is missing or the command is
  not about the shop, do not call a tool; reply in one short Hinglish sentence instead."""


# ------------------------------------------------------------------ tools ---
# Each tool function receives validated args and returns a short Hinglish reply.

def _add_udhaar(conn: sqlite3.Connection, a: CustomerAmountArgs) -> str:
    r = khata.add_udhaar(conn, a.customer_name, a.amount, a.note or None)
    return f"{r['customer']['name']} ko ₹{a.amount} udhaar likh diya. Kul baaki ₹{r['customer']['balance']}."


def _record_payment(conn: sqlite3.Connection, a: CustomerAmountArgs) -> str:
    r = khata.add_payment(conn, a.customer_name, a.amount, a.note or None)
    balance = r["customer"]["balance"]
    status = "Khata clear ho gaya!" if balance == 0 else f"Ab ₹{balance} baaki hai."
    return f"{r['customer']['name']} se ₹{a.amount} mil gaye. {status}"


def _get_balance(conn: sqlite3.Connection, a: CustomerArgs) -> str:
    customer = khata.find_customer(conn, a.customer_name)
    s = khata.customer_summary(conn, customer["id"])
    if s["balance"] == 0:
        return f"{s['name']} ka koi udhaar baaki nahi hai."
    return f"{s['name']} ka ₹{s['balance']} baaki hai, {s['days_pending']} din se."


def _list_pending(conn: sqlite3.Connection, a: NoArgs) -> str:
    pending = khata.list_pending(conn)
    if not pending:
        return "Kisi ka udhaar baaki nahi hai."
    total = sum(c["balance"] for c in pending)
    top = ", ".join(f"{c['name']} ₹{c['balance']}" for c in pending[:3])
    return f"{len(pending)} logon ka kul ₹{total} baaki hai. Sabse zyada: {top}."


def _send_reminder(conn: sqlite3.Connection, a: CustomerArgs) -> str:
    r = payments.send_reminder(conn, a.customer_name)
    return f"{r['customer']['name']} ko ₹{r['customer']['balance']} ka reminder payment link ke saath bhej diya."


def _remind_all(conn: sqlite3.Connection, a: NoArgs) -> str:
    reminded = payments.remind_all(conn)
    if not reminded:
        return "Kisi ka udhaar baaki nahi, reminder ki zaroorat nahi."
    return f"{len(reminded)} customers ko reminder bhej diya."


def _business_summary(conn: sqlite3.Connection, a: SummaryArgs) -> str:
    s = insights.business_summary(conn, a.period)
    when = {"today": "Aaj", "week": "Is hafte", "month": "Is mahine"}[a.period]
    return (f"{when} ₹{s['sales_total']} ki UPI sale hui ({s['sales_count']} bills). "
            f"₹{s['payments_received']} udhaar wapas aaya, ₹{s['udhaar_given']} naya udhaar diya.")


@dataclass
class Tool:
    description: str              # tells Gemini when to use this tool
    args_model: type[BaseModel]   # schema for Gemini AND validator for its output
    run: Callable[[sqlite3.Connection, BaseModel], str]


TOOLS: dict[str, Tool] = {
    "add_udhaar": Tool("Record credit (udhaar) given to a customer.", CustomerAmountArgs, _add_udhaar),
    "record_payment": Tool("Record money a customer paid back.", CustomerAmountArgs, _record_payment),
    "get_balance": Tool("Tell how much one customer owes.", CustomerArgs, _get_balance),
    "list_pending": Tool("List all customers who owe money.", NoArgs, _list_pending),
    "send_reminder": Tool("Send a WhatsApp payment reminder to one customer.", CustomerArgs, _send_reminder),
    "remind_all": Tool("Send WhatsApp payment reminders to everyone who owes money.", NoArgs, _remind_all),
    "business_summary": Tool("Sales and udhaar summary for today, this week or this month.", SummaryArgs, _business_summary),
}


# ------------------------------------------------------------- execution ---

def _error_reply(err: khata.KhataError, name: str) -> str:
    """Turn a business error into a short Hinglish sentence."""
    if isinstance(err, khata.CustomerNotFound):
        suggestions = err.extra.get("suggestions")
        hint = f" Kya aapka matlab {suggestions[0]} tha?" if suggestions else ""
        return f"'{name.title()}' naam ka customer nahi mila.{hint}"
    if isinstance(err, khata.AmbiguousCustomer):
        return f"'{name.title()}' naam ke kai customer hain: {' ya '.join(err.extra['candidates'])}. Poora naam boliye."
    if isinstance(err, khata.InvalidAmount) and "balance" in err.extra:
        who, balance = err.extra.get("customer", name), err.extra["balance"]
        return f"{who} ka koi udhaar baaki nahi hai." if balance == 0 else f"{who} ka sirf ₹{balance} baaki hai."
    return "Amount sahi nahi hai, dobara boliye."


def execute_tool(conn: sqlite3.Connection, name: str, raw_args: dict) -> tuple[str, dict]:
    """Validate and run one tool call. Returns (reply, args actually used).

    Never raises for bad input: whatever the LLM sends, the owner gets a
    sensible spoken reply and the database is left untouched.
    """
    tool = TOOLS.get(name)
    if tool is None:  # the LLM invented a tool that doesn't exist
        log.warning("Unknown tool requested: %s", name)
        return "Yeh kaam mujhe abhi nahi aata.", raw_args

    try:
        args = tool.args_model.model_validate(raw_args)
    except ValidationError as e:
        bad_fields = {str(err["loc"][0]) for err in e.errors() if err["loc"]}
        if "amount" in bad_fields:
            return f"Amount samajh nahi aaya. ₹1 se ₹{khata.MAX_AMOUNT} tak boliye.", raw_args
        if "customer_name" in bad_fields:
            return "Customer ka naam samajh nahi aaya, dobara boliye.", raw_args
        return "Command samajh nahi aaya, dobara boliye.", raw_args

    try:
        return tool.run(conn, args), args.model_dump()
    except khata.KhataError as e:
        return _error_reply(e, getattr(args, "customer_name", "")), args.model_dump()


# ---------------------------------------------------------------- gemini ---

def gemini_tool_declarations() -> list[types.FunctionDeclaration]:
    """Build Gemini's tool list from TOOLS, so there is one source of truth."""
    declarations = []
    for name, tool in TOOLS.items():
        schema = tool.args_model.model_json_schema()
        declarations.append(types.FunctionDeclaration(
            name=name,
            description=tool.description,
            # Tools with no arguments send no schema at all.
            parameters_json_schema=schema if schema.get("properties") else None,
        ))
    return declarations


@lru_cache(maxsize=1)
def _client(api_key: str) -> genai.Client:
    # Cached so we don't build a new HTTP client on every command.
    return genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS))


def decision_from_response(response) -> Decision | None:
    """Extract the tool call (or plain-text reply) from a Gemini response."""
    if response.function_calls:
        call = response.function_calls[0]  # we ask for one action per command
        return Decision(tool=call.name, args=dict(call.args or {}))
    if response.text:
        return Decision(reply=response.text.strip())
    return None


def ask_gemini(text: str, lang: str) -> Decision | None:
    """One Gemini call. Raises on network/API errors; the caller falls back to rules."""
    response = _client(os.environ["GEMINI_API_KEY"]).models.generate_content(
        model=os.getenv("GEMINI_MODEL") or DEFAULT_MODEL,  # "or": an empty GEMINI_MODEL= also means default
        contents=f"Speech language: {lang}\nCommand: {text}",
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[types.Tool(function_declarations=gemini_tool_declarations())],
            # We execute tools ourselves; never let the SDK auto-run anything.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            # AUTO (not ANY): lets Gemini ask "kitne rupaye?" instead of being
            # forced to guess an amount when the owner didn't say one.
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="AUTO"),
            ),
            temperature=0,  # same command -> same tool choice
        ),
    )
    return decision_from_response(response)


# ------------------------------------------------------------ entry point ---

NOT_UNDERSTOOD = "Maaf kijiye, samajh nahi aaya. Aise boliye: 'Ramesh ko 500 udhaar diya'."


def run_command(conn: sqlite3.Connection, text: str, lang: str = "hi-IN") -> dict:
    """Understand one command with Gemini (or rules as fallback) and execute it."""
    decision, brain = None, "rules"

    if os.getenv("GEMINI_API_KEY"):
        try:
            decision, brain = ask_gemini(text, lang), "gemini"
        except Exception as e:  # network down, bad key, quota, timeout...
            log.warning("Gemini failed, falling back to rules: %s", e)
            decision = None

    if decision is None:
        decision, brain = rules.parse(text), "rules"

    if decision is None:
        return {"reply": NOT_UNDERSTOOD, "tool_called": None, "args": {}, "brain": brain}

    if decision.tool is None:  # Gemini answered in words (e.g. asked for the amount)
        return {"reply": decision.reply or NOT_UNDERSTOOD, "tool_called": None, "args": {}, "brain": brain}

    reply, args = execute_tool(conn, decision.tool, decision.args)
    return {"reply": reply, "tool_called": decision.tool, "args": args, "brain": brain}
