from app import main
from tests.test_api import client_for_test


def test_metrics_endpoint_and_correlation_header(monkeypatch):
    client, engine = client_for_test(monkeypatch)
    try:
        response = client.get("/health/live", headers={"X-Correlation-ID": "corr-demo"})
        assert response.headers["X-Correlation-ID"] == "corr-demo"
        metrics = client.get("/metrics").json()
        assert metrics["service"] == "billing-service"
        assert metrics["requests_total"] >= 1
    finally:
        main.app.dependency_overrides.clear()
        engine.dispose()
