from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import Actor, get_actor
from app import main


def client_for_test(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    main.Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    main.INTERNAL_TOKEN = "test-internal"
    main.PAYMENT_PROVIDER_TOKEN = "test-provider"

    def override_db():
        with factory() as session:
            yield session

    main.app.dependency_overrides[main.get_db] = override_db
    main.app.dependency_overrides[get_actor] = lambda: Actor(is_admin=True, customer_id=None)
    monkeypatch.setattr(main.httpx, "post", lambda *args, **kwargs: type("Response", (), {"raise_for_status": lambda self: None})())
    return TestClient(main.app), engine


def test_quote_idempotency_and_total(monkeypatch):
    client, engine = client_for_test(monkeypatch)
    payload = {
        "order_id": "order-1",
        "customer_id": "customer-1",
        "items": [{"sku": "oil", "name": "Oil", "quantity": 2, "unit_price": "12.50"}],
    }
    try:
        headers = {"X-Internal-Token": "test-internal"}
        first = client.post("/quotes", json=payload, headers=headers)
        second = client.post("/quotes", json=payload, headers=headers)
        assert first.status_code == 201
        assert second.status_code == 200
        assert first.json()["total"] == "25.00"
        assert first.json()["id"] == second.json()["id"]
        assert first.json()["notification_sent"] is True
        assert first.json()["notification_channel"] == "EMAIL"
        assert first.json()["notification_sent_at"] is not None

        # Reenvio explícito do orçamento para aprovação
        resend = client.post(f"/quotes/{first.json()['id']}/send")
        assert resend.status_code == 200
        assert resend.json()["notification_sent"] is True
    finally:
        main.app.dependency_overrides.clear()
        engine.dispose()


def test_payment_must_be_verified_with_provider_token(monkeypatch):
    client, engine = client_for_test(monkeypatch)
    try:
        quote = client.post(
            "/quotes",
            json={
                "order_id": "order-2",
                "customer_id": "customer-2",
                "items": [{"sku": "filter", "name": "Filter", "quantity": 1, "unit_price": "9.00"}],
            },
            headers={"X-Internal-Token": "test-internal"},
        ).json()
        client.post(f"/quotes/{quote['id']}/approval")
        payment = client.post(f"/quotes/{quote['id']}/payments").json()
        response = client.post(
            f"/payments/{payment['id']}/verify",
            json={"status": "CONFIRMED", "provider_reference": "provider-123"},
        )
        assert response.status_code == 401
    finally:
        main.app.dependency_overrides.clear()
        engine.dispose()


def test_verified_payment_notifies_order_orchestrator(monkeypatch):
    client, engine = client_for_test(monkeypatch)
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return type("Response", (), {"raise_for_status": lambda self: None})()

    monkeypatch.setattr(main.httpx, "post", post)
    try:
        quote = client.post(
            "/quotes",
            json={
                "order_id": "order-3",
                "customer_id": "customer-3",
                "items": [{"sku": "filter", "name": "Filter", "quantity": 1, "unit_price": "9.00"}],
            },
            headers={"X-Internal-Token": "test-internal"},
        ).json()
        client.post(f"/quotes/{quote['id']}/approval")
        payment = client.post(f"/quotes/{quote['id']}/payments").json()
        response = client.post(
            f"/payments/{payment['id']}/verify",
            json={"status": "CONFIRMED", "provider_reference": "provider-123"},
            headers={"X-Provider-Token": "test-provider"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "CONFIRMED"
        assert calls[-1][1]["json"] == {"order_id": "order-3", "payment_id": payment["id"]}
    finally:
        main.app.dependency_overrides.clear()
        engine.dispose()
