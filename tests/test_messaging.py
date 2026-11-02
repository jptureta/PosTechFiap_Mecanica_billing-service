import boto3
import pytest
from moto import mock_aws
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main, messaging


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    main.Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        yield session
    engine.dispose()


@pytest.fixture
def sqs(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
        monkeypatch.setattr(messaging, "MESSAGING_ENABLED", True)
        monkeypatch.setattr(messaging, "_client", None)
        client = boto3.client("sqs", region_name="us-east-1")
        events_url = client.create_queue(
            QueueName="os-events.fifo",
            Attributes={"FifoQueue": "true"},
        )["QueueUrl"]
        commands_url = client.create_queue(
            QueueName="billing-commands.fifo",
            Attributes={"FifoQueue": "true"},
        )["QueueUrl"]
        monkeypatch.setattr(main, "OS_EVENTS_QUEUE_URL", events_url)
        monkeypatch.setattr(main, "BILLING_COMMANDS_QUEUE_URL", commands_url)
        yield {"events": events_url, "commands": commands_url}
        monkeypatch.setattr(messaging, "_client", None)


def test_create_quote_command_is_idempotent_and_emits_event(db, sqs):
    payload = {
        "order_id": "order-1",
        "customer_id": "c1",
        "items": [{"sku": "oil", "name": "Oil", "quantity": 2, "unit_price": "10.00"}],
    }
    main.handle_billing_command(db, "CreateQuote", payload)
    main.handle_billing_command(db, "CreateQuote", payload)
    quotes = db.scalars(select(main.Quote)).all()
    assert len(quotes) == 1
    assert str(quotes[0].total) == "20.00"
    events = []
    messaging.process_batch(sqs["events"], lambda t, p: events.append((t, p)), wait_seconds=0)
    assert events == [("QuoteCreated", {"order_id": "order-1", "quote_id": quotes[0].id})]


def test_refund_payment_command_marks_refund_and_emits_event(db, sqs):
    quote = main.Quote(order_id="o1", customer_id="c1", items=[], total=10)
    db.add(quote)
    db.commit()
    payment = main.Payment(quote_id=quote.id, status="CONFIRMED")
    db.add(payment)
    db.commit()
    main.handle_billing_command(db, "RefundPayment", {"order_id": "o1", "payment_id": payment.id})
    db.refresh(payment)
    assert payment.status == "REFUND_REQUIRED"
    events = []
    messaging.process_batch(sqs["events"], lambda t, p: events.append((t, p)), wait_seconds=0)
    assert events == [("PaymentRefundRequested", {"order_id": "o1", "payment_id": payment.id})]

