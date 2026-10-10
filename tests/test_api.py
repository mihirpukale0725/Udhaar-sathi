"""HTTP-level tests: status codes, response shapes, and error messages."""

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(shop_db):
    with TestClient(app) as c:
        yield c


def test_post_udhaar_fuzzy_name(client):
    r = client.post("/udhaar", json={"customer_name": "ramesh ji", "amount": 500, "note": "atta"})
    assert r.status_code == 201
    body = r.json()
    assert body["customer"]["name"] == "Ramesh Patil"
    assert body["customer"]["balance"] == 500
    assert body["entry"]["type"] == "udhaar"
    assert "Ramesh Patil" in body["message"]


@pytest.mark.parametrize("amount", [0, -50, 500.5, "five hundred", 10_000_000])
def test_post_udhaar_invalid_amount_gives_422(client, amount):
    r = client.post("/udhaar", json={"customer_name": "Ramesh", "amount": amount})
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_request"
    assert r.json()["detail"].startswith("amount:")


def test_post_udhaar_blank_name_gives_422(client):
    r = client.post("/udhaar", json={"customer_name": "   ", "amount": 100})
    assert r.status_code == 422
    assert "customer_name" in r.json()["detail"]


def test_unknown_customer_gives_404_with_suggestions(client):
    r = client.post("/udhaar", json={"customer_name": "Rajesh", "amount": 100})
    assert r.status_code == 404
    assert r.json()["error"] == "customer_not_found"
    assert "Ramesh Patil" in r.json()["suggestions"]


def test_ambiguous_customer_gives_409(client, shop_db):
    shop_db.execute("INSERT INTO customers (name) VALUES ('Ramesh Gupta')")
    shop_db.commit()
    r = client.post("/udhaar", json={"customer_name": "Ramesh", "amount": 100})
    assert r.status_code == 409
    assert len(r.json()["candidates"]) == 2


def test_overpayment_gives_400(client):
    client.post("/udhaar", json={"customer_name": "Suresh", "amount": 300})
    r = client.post("/payment", json={"customer_name": "Suresh", "amount": 1000})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_amount"
    assert r.json()["balance"] == 300


def test_payment_closes_khata(client):
    client.post("/udhaar", json={"customer_name": "Suresh", "amount": 300})
    r = client.post("/payment", json={"customer_name": "Suresh", "amount": 300})
    assert r.status_code == 201
    assert r.json()["customer"]["balance"] == 0
    assert client.get("/khata").json()["customers"] == []


def test_get_khata_sorted_with_total(client):
    client.post("/udhaar", json={"customer_name": "Ramesh", "amount": 200})
    client.post("/udhaar", json={"customer_name": "Sunita", "amount": 700})
    body = client.get("/khata").json()
    assert body["total_pending"] == 900
    assert [c["name"] for c in body["customers"]] == ["Sunita Deshmukh", "Ramesh Patil"]
    assert body["customers"][0]["days_pending"] == 0


def test_get_customer_with_history(client):
    client.post("/udhaar", json={"customer_name": "Ramesh", "amount": 500})
    client.post("/payment", json={"customer_name": "Ramesh", "amount": 200})
    body = client.get("/customer/1").json()
    assert body["balance"] == 300
    assert [(e["type"], e["balance_after"]) for e in body["ledger"]] == [("udhaar", 500), ("payment", 300)]


def test_get_unknown_customer_gives_404(client):
    r = client.get("/customer/999")
    assert r.status_code == 404
    assert r.json()["error"] == "customer_not_found"


def test_command_endpoint(client):
    r = client.post("/command", json={"text": "Ramesh ko 500 udhaar diya", "lang": "hi-IN"})
    assert r.status_code == 200
    assert r.json() == {
        "reply": "Ramesh Patil ko ₹500 udhaar likh diya. Kul baaki ₹500.",
        "tool_called": "add_udhaar",
        "args": {"customer_name": "ramesh", "amount": 500, "note": ""},
        "brain": "rules",
    }


def test_command_lang_defaults_and_is_validated(client):
    assert client.post("/command", json={"text": "aaj kitna aaya"}).status_code == 200
    r = client.post("/command", json={"text": "aaj kitna aaya", "lang": "fr-FR"})
    assert r.status_code == 422
    assert r.json()["detail"].startswith("lang:")


# ----------------------------------------------------------- dashboard ---

def test_index_serves_dashboard(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Udhaar" in r.text and "webkitSpeechRecognition" in r.text


def test_summary(client):
    client.post("/udhaar", json={"customer_name": "Ramesh", "amount": 500})
    client.post("/payment", json={"customer_name": "Ramesh", "amount": 200})
    body = client.get("/summary").json()
    assert body["udhaar_given"] == 500
    assert body["payments_received"] == 200
    assert body["total_pending"] == 300
    assert client.get("/summary?period=year").status_code == 422


def test_activity_newest_first(client):
    client.post("/udhaar", json={"customer_name": "Ramesh", "amount": 500, "note": "atta"})
    client.post("/payment", json={"customer_name": "Ramesh", "amount": 200})
    items = client.get("/activity").json()
    assert [(i["type"], i["amount"]) for i in items] == [("payment", 200), ("udhaar", 500)]
    assert items[1]["customer_name"] == "Ramesh Patil" and items[1]["note"] == "atta"


def test_remind_button_by_id(client):
    client.post("/udhaar", json={"customer_name": "Suresh", "amount": 400})
    r = client.post("/customer/3/remind")
    assert r.status_code == 200
    assert r.json()["reply"] == "Suresh Gupta ko ₹400 ka reminder bhej diya."
    assert "/pay/" in r.json()["link"]
    assert client.get("/messages").json()[0]["kind"] == "reminder"


def test_remind_button_errors(client):
    assert client.post("/customer/2/remind").status_code == 400    # Sunita owes nothing
    assert client.post("/customer/999/remind").status_code == 404
