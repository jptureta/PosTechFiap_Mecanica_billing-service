from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt

from app import auth, main, messaging
from app.auth import Actor, get_actor
from tests.test_api import client_for_test


def cred(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_auth_flows(monkeypatch):
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    with pytest.raises(HTTPException) as exc:
        auth.get_actor(cred("x"))
    assert exc.value.status_code == 503

    monkeypatch.setenv("JWT_SECRET_KEY", "s1")
    with pytest.raises(HTTPException) as exc:
        auth.get_actor(cred("invalid"))
    assert exc.value.status_code == 401

    admin_tok = jwt.encode({"sub": "a", "tipo": "admin"}, "s1", algorithm="HS256")
    assert auth.get_actor(cred(admin_tok)).is_admin is True

    cust_tok = jwt.encode({"sub": "c", "tipo": "cliente", "cliente_id": "c1"}, "s1", algorithm="HS256")
    assert auth.get_actor(cred(cust_tok)) == Actor(is_admin=False, customer_id="c1")

    with pytest.raises(HTTPException):
        auth.get_actor(cred(jwt.encode({"sub": "c", "tipo": "cliente"}, "s1", algorithm="HS256")))
    with pytest.raises(HTTPException):
        auth.get_actor(cred(jwt.encode({"sub": "c", "tipo": "x"}, "s1", algorithm="HS256")))


def test_health_metrics_rejection_compensation_and_mercado_pago(monkeypatch):
    client, engine = client_for_test(monkeypatch)
    try:
        assert client.get("/health").status_code == 200
        assert client.get("/health/live").status_code == 200
        assert client.get("/metrics").json()["service"] == "billing-service"

        headers = {"X-Internal-Token": "test-internal"}
        quote = client.post(
            "/quotes",
            json={
                "order_id": "ord-mp",
                "customer_id": "cust-1",
                "items": [{"sku": "oil", "name": "Oil", "quantity": 1, "unit_price": "50.00"}],
            },
            headers=headers,
        ).json()
        assert client.get(f"/quotes/{quote['id']}").status_code == 200

        # Configure Mercado Pago QR creation and webhook
        monkeypatch.setattr(main, "MERCADO_PAGO_ACCESS_TOKEN", "mp-token")
        monkeypatch.setattr(main, "MERCADO_PAGO_USER_ID", "seller-1")
        monkeypatch.setattr(main, "MERCADO_PAGO_POS_ID", "POS1")
        monkeypatch.setattr(main, "MERCADO_PAGO_NOTIFICATION_URL", "https://example.com/hook")
        monkeypatch.setattr(
            main.httpx,
            "post",
            lambda url, **kw: SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"qr_data": "00020101021243650016COM.MERCADOLIBRE"},
            ),
        )

        client.post(f"/quotes/{quote['id']}/approval")
        payment = client.post(f"/quotes/{quote['id']}/payments").json()
        assert payment["provider"] == "MERCADO_PAGO"
        assert payment["qr_data"].startswith("000201")

        # Webhook ignore unrelated topic
        assert client.post("/payments/mercado-pago/webhook", json={"type": "other"}).json() == {"received": True}

        # Webhook approved merchant order
        monkeypatch.setattr(
            main.httpx,
            "get",
            lambda url, **kw: SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "external_reference": payment["id"],
                    "payments": [{"id": "mp-pay-99", "status": "approved"}],
                },
            ),
        )
        hook = client.post("/payments/mercado-pago/webhook", json={"type": "merchant_order", "id": "mo-1"})
        assert hook.status_code == 200

        # Compensation endpoint
        comp = client.post(f"/internal/payments/{payment['id']}/compensate", headers=headers)
        assert comp.status_code == 200
        assert comp.json()["status"] == "REFUND_REQUIRED"

        # Quote rejection flow with messaging enabled
        monkeypatch.setattr(messaging, "MESSAGING_ENABLED", True)
        monkeypatch.setattr(main, "emit_os_event", lambda *a, **kw: None)
        q2 = client.post(
            "/quotes",
            json={
                "order_id": "ord-rej",
                "customer_id": "cust-1",
                "items": [{"sku": "oil", "name": "Oil", "quantity": 1, "unit_price": "30.00"}],
            },
            headers=headers,
        ).json()
        rej = client.post(f"/quotes/{q2['id']}/rejection")
        assert rej.status_code == 200
        assert rej.json()["status"] == "REJECTED"

        # Customer access restriction
        main.app.dependency_overrides[get_actor] = lambda: Actor(is_admin=False, customer_id="other")
        assert client.get(f"/quotes/{q2['id']}").status_code == 403
    finally:
        main.app.dependency_overrides.clear()
        engine.dispose()

