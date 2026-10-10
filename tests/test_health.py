from fastapi.testclient import TestClient

from app.main import app


def test_health_returns_ok():
    # Using TestClient as a context manager runs the startup (lifespan) code too.
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
