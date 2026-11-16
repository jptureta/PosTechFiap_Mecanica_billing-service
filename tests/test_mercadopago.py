"""Mercado Pago: QR Code dinâmico in-store e webhook merchant_order.

Cobre os dois modos de aviso ao orquestrador: RabbitMQ (MESSAGING_ENABLED=true)
e o caminho REST original (/internal/payments/confirmed no OS).
"""

from types import SimpleNamespace

import httpx
import pytest

from app import main
from tests.test_api import client_for_test

HEADERS = {"X-Internal-Token": "test-internal"}


def ok(payload):
    return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)


def http_error(*_, **__):
    raise httpx.ConnectError("mercado pago down")


@pytest.fixture
def mp(monkeypatch):
    client, engine = client_for_test(monkeypatch)
    events = []
    monkeypatch.setattr(main.messaging, "MESSAGING_ENABLED", True)
    monkeypatch.setattr(main.messaging, "publish", lambda queue, kind, payload: events.append((queue, kind, payload)))
    monkeypatch.setattr(main, "MERCADO_PAGO_ACCESS_TOKEN", "mp-token")
    monkeypatch.setattr(main, "MERCADO_PAGO_USER_ID", "seller-1")
    monkeypatch.setattr(main, "MERCADO_PAGO_POS_ID", "POS1")
    monkeypatch.setattr(main, "MERCADO_PAGO_NOTIFICATION_URL", "https://example.com/hook")
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return ok({"qr_data": "00020101021243650016COM.MERCADOLIBRE"})

    monkeypatch.setattr(main.httpx, "post", fake_post)
    yield SimpleNamespace(client=client, events=events, calls=calls, monkeypatch=monkeypatch)
    main.app.dependency_overrides.clear()
    engine.dispose()


def approved_quote(client, order_id="ord-mp"):
    quote = client.post(
        "/quotes",
        json={
            "order_id": order_id,
            "customer_id": "cust-1",
            "items": [
                {"kind": "PART", "sku": "oil", "name": "Oil", "quantity": 2, "unit_price": "50.00"},
                {"kind": "SERVICE", "sku": "labor", "name": "Troca", "quantity": 1, "unit_price": "30.00"},
            ],
        },
        headers=HEADERS,
    ).json()
    assert client.post(f"/quotes/{quote['id']}/approval").status_code == 200
    return quote


def merchant_order(monkeypatch, external_reference, statuses):
    payments = [{"id": f"mp-pay-{i}", "status": status} for i, status in enumerate(statuses)]
    monkeypatch.setattr(
        main.httpx, "get", lambda url, **kw: ok({"external_reference": external_reference, "payments": payments})
    )


def hook(client, merchant_order_id="mo-1"):
    return client.post("/payments/mercado-pago/webhook", json={"type": "merchant_order", "id": merchant_order_id})


def saga_events(mp):
    return [(kind, payload) for _, kind, payload in mp.events if kind.startswith("Payment")]


# --- geração do QR ---------------------------------------------------------------


def test_payment_generates_dynamic_qr_once(mp):
    quote = approved_quote(mp.client)
    payment = mp.client.post(f"/quotes/{quote['id']}/payments").json()
    again = mp.client.post(f"/quotes/{quote['id']}/payments").json()

    assert payment["provider"] == "MERCADO_PAGO"
    assert payment["qr_data"].startswith("000201")
    assert again["id"] == payment["id"]
    qr_calls = [call for call in mp.calls if "/instore/qr/" in call[0]]
    assert len(qr_calls) == 1
    url, kwargs = qr_calls[0]
    assert url == "https://api.mercadopago.com/instore/qr/seller/collectors/seller-1/pos/POS1/qrs"
    assert kwargs["headers"] == {"Authorization": "Bearer mp-token"}
    body = kwargs["json"]
    assert body["external_reference"] == payment["id"]
    assert body["total_amount"] == 130.0
    assert body["notification_url"] == "https://example.com/hook"
    assert [item["total_amount"] for item in body["items"]] == [100.0, 30.0]
    assert [item["category"] for item in body["items"]] == ["PART", "SERVICE"]
    assert body["expiration_date"].endswith("Z")


def test_payment_without_mercado_pago_keeps_manual_mode(mp):
    mp.monkeypatch.setattr(main, "MERCADO_PAGO_ACCESS_TOKEN", "")
    quote = approved_quote(mp.client)
    payment = mp.client.post(f"/quotes/{quote['id']}/payments").json()
    assert payment["status"] == "PENDING"
    assert payment["qr_data"] is None
    assert not [call for call in mp.calls if "/instore/qr/" in call[0]]


def test_qr_requires_complete_configuration(mp):
    mp.monkeypatch.setattr(main, "MERCADO_PAGO_POS_ID", "")
    quote = approved_quote(mp.client)
    assert mp.client.post(f"/quotes/{quote['id']}/payments").status_code == 503


def test_qr_errors_from_provider(mp):
    quote = approved_quote(mp.client)
    mp.monkeypatch.setattr(main.httpx, "post", http_error)
    assert mp.client.post(f"/quotes/{quote['id']}/payments").status_code == 502
    mp.monkeypatch.setattr(main.httpx, "post", lambda url, **kw: ok({}))
    assert mp.client.post(f"/quotes/{quote['id']}/payments").status_code == 502


# --- webhook com RabbitMQ ------------------------------------------------------------


def test_webhook_approved_publishes_payment_confirmed(mp):
    quote = approved_quote(mp.client)
    payment = mp.client.post(f"/quotes/{quote['id']}/payments").json()
    merchant_order(mp.monkeypatch, payment["id"], ["rejected", "approved"])

    assert hook(mp.client).json() == {"received": True}
    queue, kind, payload = mp.events[-1]
    assert queue == main.messaging.OS_EVENTS_QUEUE
    assert (kind, payload) == (
        "PaymentConfirmed",
        {"order_id": "ord-mp", "quote_id": quote["id"], "payment_id": payment["id"]},
    )
    # Notificação repetida é ignorada: nada é republicado.
    assert hook(mp.client).status_code == 200
    assert [kind for kind, _ in saga_events(mp)].count("PaymentConfirmed") == 1


def test_webhook_rejected_order_publishes_payment_failed(mp):
    quote = approved_quote(mp.client)
    payment = mp.client.post(f"/quotes/{quote['id']}/payments").json()
    merchant_order(mp.monkeypatch, payment["id"], ["rejected", "cancelled"])
    assert hook(mp.client).status_code == 200
    assert saga_events(mp)[-1][0] == "PaymentFailed"


def test_webhook_ignores_pending_unknown_and_other_topics(mp):
    quote = approved_quote(mp.client)
    payment = mp.client.post(f"/quotes/{quote['id']}/payments").json()
    assert mp.client.post("/payments/mercado-pago/webhook", json={"type": "other"}).json() == {"received": True}
    assert mp.client.post("/payments/mercado-pago/webhook", json={"type": "merchant_order"}).json() == {
        "received": True
    }
    merchant_order(mp.monkeypatch, payment["id"], ["pending"])
    assert hook(mp.client).json() == {"received": True}
    merchant_order(mp.monkeypatch, "unknown-payment", ["approved"])
    assert hook(mp.client).json() == {"received": True}
    assert saga_events(mp) == []


def test_webhook_accepts_topic_and_data_id_formats(mp):
    quote = approved_quote(mp.client)
    payment = mp.client.post(f"/quotes/{quote['id']}/payments").json()
    merchant_order(mp.monkeypatch, payment["id"], ["approved"])
    response = mp.client.post(
        "/payments/mercado-pago/webhook", json={"topic": "merchant_order", "data": {"id": "mo-2"}}
    )
    assert response.status_code == 200
    assert saga_events(mp)[-1][0] == "PaymentConfirmed"


def test_webhook_errors(mp):
    mp.monkeypatch.setattr(main.httpx, "get", http_error)
    assert hook(mp.client).status_code == 502
    mp.monkeypatch.setattr(main, "MERCADO_PAGO_ACCESS_TOKEN", "")
    assert hook(mp.client).status_code == 503


def test_webhook_returns_503_when_saga_event_cannot_be_published(mp):
    quote = approved_quote(mp.client)
    payment = mp.client.post(f"/quotes/{quote['id']}/payments").json()
    merchant_order(mp.monkeypatch, payment["id"], ["approved"])

    def broker_down(*_):
        raise RuntimeError("broker down")

    mp.monkeypatch.setattr(main.messaging, "publish", broker_down)
    assert hook(mp.client).status_code == 503


# --- webhook sem mensageria (caminho REST original) ----------------------------------


def test_webhook_without_messaging_notifies_os_by_rest(mp):
    quote = approved_quote(mp.client)
    payment = mp.client.post(f"/quotes/{quote['id']}/payments").json()
    merchant_order(mp.monkeypatch, payment["id"], ["approved"])
    mp.monkeypatch.setattr(main.messaging, "MESSAGING_ENABLED", False)
    notified = []
    mp.monkeypatch.setattr(main.httpx, "post", lambda url, **kw: notified.append((url, kw["json"])) or ok({}))

    assert hook(mp.client).status_code == 200
    assert notified == [
        ("http://os-service:8000/internal/payments/confirmed", {"order_id": "ord-mp", "payment_id": payment["id"]})
    ]


def test_webhook_without_messaging_does_not_call_os_on_decline(mp):
    quote = approved_quote(mp.client)
    payment = mp.client.post(f"/quotes/{quote['id']}/payments").json()
    merchant_order(mp.monkeypatch, payment["id"], ["rejected"])
    mp.monkeypatch.setattr(main.messaging, "MESSAGING_ENABLED", False)
    notified = []
    mp.monkeypatch.setattr(main.httpx, "post", lambda url, **kw: notified.append(url) or ok({}))
    assert hook(mp.client).status_code == 200
    assert notified == []


def test_webhook_without_messaging_returns_503_when_os_is_down(mp):
    quote = approved_quote(mp.client)
    payment = mp.client.post(f"/quotes/{quote['id']}/payments").json()
    merchant_order(mp.monkeypatch, payment["id"], ["approved"])
    mp.monkeypatch.setattr(main.messaging, "MESSAGING_ENABLED", False)
    mp.monkeypatch.setattr(main.httpx, "post", http_error)
    assert hook(mp.client).status_code == 503


def test_compensation_after_mercado_pago_payment(mp):
    quote = approved_quote(mp.client)
    payment = mp.client.post(f"/quotes/{quote['id']}/payments").json()
    merchant_order(mp.monkeypatch, payment["id"], ["approved"])
    hook(mp.client)
    comp = mp.client.post(f"/internal/payments/{payment['id']}/compensate", headers=HEADERS)
    assert comp.json() == {"payment_id": payment["id"], "status": "REFUND_REQUIRED"}
