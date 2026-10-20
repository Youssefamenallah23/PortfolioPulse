"""Health endpoint behaviour."""

from fastapi.testclient import TestClient

from portfoliopulse.main import app


def test_health_response_schema():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "portfoliopulse"
    assert body["environment"]

    # Health must not leak secrets or machine-specific paths.
    raw = response.text
    for leak in ("key", "secret", "token", "\\", "C:"):
        assert leak.lower() not in raw.lower(), f"health response leaks '{leak}': {raw}"
