# Udhaar Saathi

A voice-first AI udhaar (credit) assistant for Indian kirana stores. The shop owner
speaks in Hindi, Marathi or English (*"Ramesh ko 500 udhaar diya"*), and an AI agent
updates the khata, sends a WhatsApp reminder with a UPI payment link, and closes the
entry automatically when the customer pays.

> 🚧 Work in progress. Done so far: project skeleton, database schema, demo seed data.

## ⚠️ What is mocked

This is a portfolio project. No real messages are sent and no real money moves.

| Real-world service | What this project does instead |
|---|---|
| **WhatsApp** | Messages are saved in the `messages` table and shown in an on-screen "customer phone" panel. |
| **UPI / Razorpay** | Our own fake `/pay/{token}` page. Clicking "Pay" calls our webhook, signed with HMAC-SHA256 exactly as a real gateway would. |

All customer names and phone numbers in the seed data are fictional (`+91980000000x`).

## Stack

Python · FastAPI · Pydantic · SQLite (plain `sqlite3`) · Google Gemini (`google-genai`,
function calling) with a regex Hinglish fallback · vanilla JS + Web Speech API · pytest

## Run locally (Windows PowerShell)

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env          # then fill in GEMINI_API_KEY (optional for now)
python -m app.seed              # creates udhaar.db with demo data
uvicorn app.main:app --reload   # http://127.0.0.1:8000/health
pytest -v
```

On macOS/Linux use `python3 -m venv .venv`, `source .venv/bin/activate` and `cp`.
