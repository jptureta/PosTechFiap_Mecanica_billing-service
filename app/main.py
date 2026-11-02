import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Literal
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, Numeric, String, Text, create_engine, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app import messaging, observability
from app.auth import Actor, get_actor

logger = logging.getLogger(__name__)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./billing-service.db")
OS_SERVICE_URL = os.getenv("OS_SERVICE_URL", "http://os-service:8000").rstrip("/")
OS_EVENTS_QUEUE_URL = os.getenv("OS_EVENTS_QUEUE_URL", "")
BILLING_COMMANDS_QUEUE_URL = os.getenv("BILLING_COMMANDS_QUEUE_URL", "")
INTERNAL_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "")
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "")
MERCADO_PAGO_ACCESS_TOKEN = os.getenv("MERCADO_PAGO_ACCESS_TOKEN", "")
MERCADO_PAGO_USER_ID = os.getenv("MERCADO_PAGO_USER_ID", "")
MERCADO_PAGO_POS_ID = os.getenv("MERCADO_PAGO_POS_ID", "")
MERCADO_PAGO_API_URL = os.getenv("MERCADO_PAGO_API_URL", "https://api.mercadopago.com").rstrip("/")
MERCADO_PAGO_NOTIFICATION_URL = os.getenv("MERCADO_PAGO_NOTIFICATION_URL", "")
engine_options = {"connect_args": {"check_same_thread": False}} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, **engine_options)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Quote(Base):
    __tablename__ = "quotes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    order_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, index=True)
    customer_id: Mapped[str] = mapped_column(String(100), nullable=False)
    items: Mapped[list] = mapped_column(JSON, nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="PENDING_APPROVAL")
    notification_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notification_channel: Mapped[str | None] = mapped_column(String(50), nullable=True, default="EMAIL")
    notification_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    quote_id: Mapped[str] = mapped_column(ForeignKey("quotes.id"), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="PENDING")
    provider_reference: Mapped[str | None] = mapped_column(String(200))
    qr_data: Mapped[str | None] = mapped_column(Text())
    provider: Mapped[str | None] = mapped_column(String(50))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class QuoteItem(BaseModel):
    kind: Literal["PART", "SERVICE"] = "PART"
    sku: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    quantity: int = Field(gt=0)
    unit_price: Decimal = Field(gt=0)


class CreateQuote(BaseModel):
    order_id: str
    customer_id: str
    items: list[QuoteItem] = Field(min_length=1)


class QuoteResponse(BaseModel):
    id: str
    order_id: str
    customer_id: str
    items: list[dict]
    total: Decimal
    status: str
    notification_sent: bool = False
    notification_channel: str | None = "EMAIL"
    notification_sent_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class PaymentVerification(BaseModel):
    status: str
    provider_reference: str = Field(min_length=1, max_length=200)


def get_db():
    with SessionLocal() as session:
        yield session


def require_internal_token(x_internal_token: Annotated[str | None, Header()] = None) -> None:
    if not INTERNAL_TOKEN or x_internal_token != INTERNAL_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid internal service token")


def ensure_quote_access(quote: Quote, actor: Actor) -> None:
    if not actor.is_admin and actor.customer_id != quote.customer_id:
        raise HTTPException(status_code=403, detail="quote does not belong to the authenticated customer")


def dispatch_quote_notification(quote: Quote) -> bool:
    """Dispara o envio de notificação do orçamento para aprovação do cliente (email/webhook)."""
    logger.info(
        "Orçamento %s (OS %s) enviado com sucesso para o cliente %s. Total: R$ %s via %s",
        quote.id,
        quote.order_id,
        quote.customer_id,
        quote.total,
        quote.notification_channel or "EMAIL",
    )
    quote.notification_sent = True
    quote.notification_sent_at = datetime.now(timezone.utc)
    return True


def emit_os_event(event_type: str, payload: dict, order_id: str, dedup_id: str | None = None) -> None:
    if messaging.MESSAGING_ENABLED and OS_EVENTS_QUEUE_URL:
        messaging.publish(OS_EVENTS_QUEUE_URL, event_type, payload, group_id=order_id, dedup_id=dedup_id)


def handle_billing_command(db: Session, command_type: str, payload: dict) -> None:
    if command_type == "CreateQuote":
        cmd = CreateQuote.model_validate(payload)
        quote = db.scalar(select(Quote).where(Quote.order_id == cmd.order_id))
        item_data = [item.model_dump(mode="json") for item in cmd.items]
        if quote is None:
            total = sum((item.unit_price * item.quantity for item in cmd.items), Decimal("0.00"))
            quote = Quote(
                order_id=cmd.order_id,
                customer_id=cmd.customer_id,
                items=item_data,
                total=total,
            )
            dispatch_quote_notification(quote)
            db.add(quote)
            db.commit()
            db.refresh(quote)
        emit_os_event(
            "QuoteCreated",
            {"order_id": quote.order_id, "quote_id": quote.id},
            order_id=quote.order_id,
            dedup_id=f"quote-created-{quote.id}",
        )
    elif command_type == "RefundPayment":
        payment_id = payload.get("payment_id")
        order_id = payload.get("order_id", "")
        payment = db.get(Payment, payment_id) if payment_id else None
        if payment is not None and payment.status != "DECLINED":
            payment.status = "REFUND_REQUIRED"
            db.commit()
        if order_id:
            emit_os_event(
                "PaymentRefundRequested",
                {"order_id": order_id, "payment_id": payment_id},
                order_id=order_id,
                dedup_id=f"refund-{payment_id}",
            )


def consume_billing_command(command_type: str, payload: dict) -> None:
    with SessionLocal() as session:
        handle_billing_command(session, command_type, payload)


@asynccontextmanager
async def lifespan(_: FastAPI):
    stops: list[threading.Event] = []
    if messaging.MESSAGING_ENABLED and BILLING_COMMANDS_QUEUE_URL:
        stops.append(messaging.start_consumer(BILLING_COMMANDS_QUEUE_URL, consume_billing_command))
    yield
    for stop in stops:
        stop.set()


app = FastAPI(title="Orçamento e Pagamento", version="1.0.0", lifespan=lifespan)
observability.instrument_app(app, "billing-service")


@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        logger.exception("database readiness check failed")
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ok", "service": "billing-service"}


@app.get("/health/live")
def liveness():
    return {"status": "ok", "service": "billing-service"}


@app.post("/quotes", status_code=status.HTTP_201_CREATED, response_model=QuoteResponse)
def create_quote(
    payload: CreateQuote,
    response: Response,
    db: Session = Depends(get_db),
    _: None = Depends(require_internal_token),
):
    quote = db.scalar(select(Quote).where(Quote.order_id == payload.order_id))
    item_data = [item.model_dump(mode="json") for item in payload.items]
    if quote is not None:
        if quote.items != item_data or quote.customer_id != payload.customer_id:
            raise HTTPException(status_code=409, detail="order already has a different quote")
        response.status_code = status.HTTP_200_OK
        return quote
    total = sum((item.unit_price * item.quantity for item in payload.items), Decimal("0.00"))
    quote = Quote(
        order_id=payload.order_id,
        customer_id=payload.customer_id,
        items=item_data,
        total=total,
    )
    dispatch_quote_notification(quote)
    db.add(quote)
    db.commit()
    db.refresh(quote)
    return quote


@app.post("/quotes/{quote_id}/send", response_model=QuoteResponse)
def send_quote_to_customer(
    quote_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
):
    quote = db.get(Quote, quote_id)
    if quote is None:
        raise HTTPException(status_code=404, detail="quote not found")
    ensure_quote_access(quote, actor)
    dispatch_quote_notification(quote)
    db.commit()
    db.refresh(quote)
    return quote


@app.post("/quotes/{quote_id}/approval")
def approve_quote(
    quote_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
):
    quote = db.get(Quote, quote_id)
    if quote is None:
        raise HTTPException(status_code=404, detail="quote not found")
    ensure_quote_access(quote, actor)
    if quote.status not in {"PENDING_APPROVAL", "APPROVED"}:
        raise HTTPException(status_code=409, detail="quote cannot be approved in its current state")
    quote.status = "APPROVED"
    db.commit()
    if messaging.MESSAGING_ENABLED:
        emit_os_event("QuoteApproved", {"order_id": quote.order_id, "quote_id": quote.id}, order_id=quote.order_id, dedup_id=f"quote-approved-{quote.id}")
        return {"quote_id": quote.id, "status": quote.status}
    try:
        response = httpx.post(
            f"{OS_SERVICE_URL}/internal/orders/{quote.order_id}/quote-approved",
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            timeout=5,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.exception("could not notify OS about quote %s", quote.id)
        raise HTTPException(status_code=503, detail="quote approved; OS notification can be retried") from exc
    return {"quote_id": quote.id, "status": quote.status}


@app.post("/quotes/{quote_id}/rejection")
def reject_quote(
    quote_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
):
    quote = db.get(Quote, quote_id)
    if quote is None:
        raise HTTPException(status_code=404, detail="quote not found")
    ensure_quote_access(quote, actor)
    if quote.status not in {"PENDING_APPROVAL", "REJECTED"}:
        raise HTTPException(status_code=409, detail="quote cannot be rejected in its current state")
    quote.status = "REJECTED"
    db.commit()
    if messaging.MESSAGING_ENABLED:
        emit_os_event("QuoteRejected", {"order_id": quote.order_id, "quote_id": quote.id}, order_id=quote.order_id, dedup_id=f"quote-rejected-{quote.id}")
    return {"quote_id": quote.id, "status": quote.status}


@app.get("/quotes/{quote_id}", response_model=QuoteResponse)
def get_quote(
    quote_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
):
    quote = db.get(Quote, quote_id)
    if quote is None:
        raise HTTPException(status_code=404, detail="quote not found")
    ensure_quote_access(quote, actor)
    return quote


def create_mercado_pago_qr(payment: Payment, quote: Quote) -> str:
    if not all((MERCADO_PAGO_ACCESS_TOKEN, MERCADO_PAGO_USER_ID, MERCADO_PAGO_POS_ID)):
        raise HTTPException(status_code=503, detail="Mercado Pago QR configuration is incomplete")

    items = []
    for index, item in enumerate(quote.items):
        quantity = int(item["quantity"])
        unit_price = Decimal(str(item["unit_price"])).quantize(Decimal("0.01"))
        items.append({
            "sku_number": str(item.get("sku", index)),
            "category": str(item.get("kind", "SERVICE")),
            "title": item["name"],
            "description": item["name"],
            "unit_price": float(unit_price),
            "quantity": quantity,
            "unit_measure": "unit",
            "total_amount": float(unit_price * quantity),
        })

    body = {
        "external_reference": payment.id,
        "title": f"Ordem de serviço {quote.order_id}",
        "description": f"Pagamento do orçamento {quote.id}",
        "total_amount": float(Decimal(str(quote.total)).quantize(Decimal("0.01"))),
        "items": items,
        "expiration_date": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
    }
    if MERCADO_PAGO_NOTIFICATION_URL:
        body["notification_url"] = MERCADO_PAGO_NOTIFICATION_URL

    url = f"{MERCADO_PAGO_API_URL}/instore/qr/seller/collectors/{MERCADO_PAGO_USER_ID}/pos/{MERCADO_PAGO_POS_ID}/qrs"
    try:
        response = httpx.post(url, headers={"Authorization": f"Bearer {MERCADO_PAGO_ACCESS_TOKEN}"}, json=body, timeout=15)
        response.raise_for_status()
        qr_data = response.json().get("qr_data")
        if not qr_data:
            raise HTTPException(status_code=502, detail="Mercado Pago did not return qr_data")
        return qr_data
    except httpx.HTTPError as exc:
        logger.exception("could not create Mercado Pago QR for payment %s", payment.id)
        raise HTTPException(status_code=502, detail="Mercado Pago QR could not be created") from exc


@app.post("/quotes/{quote_id}/payments", status_code=status.HTTP_201_CREATED)
def register_payment(
    quote_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
):
    quote = db.get(Quote, quote_id)
    if quote is None:
        raise HTTPException(status_code=404, detail="quote not found")
    ensure_quote_access(quote, actor)
    if quote.status != "APPROVED":
        raise HTTPException(status_code=409, detail="quote must be approved before payment")
    payment = db.scalar(select(Payment).where(Payment.quote_id == quote.id))
    if payment is None:
        payment = Payment(quote_id=quote.id)
        db.add(payment)
        db.commit()
        db.refresh(payment)
    if MERCADO_PAGO_ACCESS_TOKEN and not payment.qr_data:
        payment.qr_data = create_mercado_pago_qr(payment, quote)
        payment.provider = "MERCADO_PAGO"
        db.commit()
        db.refresh(payment)
    return payment


@app.post("/payments/{payment_id}/verify")
def verify_payment(
    payment_id: str,
    payload: PaymentVerification,
    db: Session = Depends(get_db),
    x_provider_token: Annotated[str | None, Header()] = None,
):
    if not PAYMENT_PROVIDER_TOKEN or x_provider_token != PAYMENT_PROVIDER_TOKEN:
        raise HTTPException(status_code=401, detail="invalid payment provider token")
    if payload.status not in {"CONFIRMED", "DECLINED"}:
        raise HTTPException(status_code=422, detail="status must be CONFIRMED or DECLINED")
    payment = db.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="payment not found")
    if payment.status == "REFUND_REQUIRED":
        raise HTTPException(status_code=409, detail="payment is awaiting refund reconciliation")
    if payment.status not in {"PENDING", payload.status}:
        raise HTTPException(status_code=409, detail="payment has already reached a different final state")
    payment.status = payload.status
    payment.provider_reference = payload.provider_reference
    payment.verified_at = datetime.now(timezone.utc)
    db.commit()
    quote = db.get(Quote, payment.quote_id)
    if messaging.MESSAGING_ENABLED:
        event_name = "PaymentConfirmed" if payload.status == "CONFIRMED" else "PaymentFailed"
        emit_os_event(
            event_name,
            {"order_id": quote.order_id, "payment_id": payment.id},
            order_id=quote.order_id,
            dedup_id=f"{event_name}-{payment.id}",
        )
        db.refresh(payment)
        return payment
    if payload.status == "CONFIRMED":
        try:
            response = httpx.post(
                f"{OS_SERVICE_URL}/internal/payments/confirmed",
                json={"order_id": quote.order_id, "payment_id": payment.id},
                headers={"X-Internal-Token": INTERNAL_TOKEN},
                timeout=5,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.exception("payment %s verified; OS saga notification failed", payment.id)
            raise HTTPException(status_code=503, detail="payment verified; saga notification can be retried") from exc
        db.refresh(payment)
    return payment


@app.post("/payments/mercado-pago/webhook", status_code=status.HTTP_200_OK)
def mercado_pago_webhook(payload: dict, db: Session = Depends(get_db)):
    if payload.get("type") not in {"merchant_order", "topic"} and payload.get("topic") != "merchant_order":
        return {"received": True}
    merchant_order_id = payload.get("id") or (payload.get("data") or {}).get("id")
    if not merchant_order_id:
        return {"received": True}
    if not MERCADO_PAGO_ACCESS_TOKEN:
        raise HTTPException(status_code=503, detail="Mercado Pago is not configured")

    try:
        response = httpx.get(
            f"{MERCADO_PAGO_API_URL}/merchant_orders/{merchant_order_id}",
            headers={"Authorization": f"Bearer {MERCADO_PAGO_ACCESS_TOKEN}"},
            timeout=15,
        )
        response.raise_for_status()
        merchant_order = response.json()
    except httpx.HTTPError as exc:
        logger.exception("could not retrieve Mercado Pago merchant order %s", merchant_order_id)
        raise HTTPException(status_code=502, detail="Mercado Pago merchant order could not be retrieved") from exc

    payment_id = merchant_order.get("external_reference")
    payment = db.get(Payment, str(payment_id)) if payment_id else None
    if payment is None:
        return {"received": True}

    payment_statuses = [item.get("status") for item in merchant_order.get("payments", [])]
    if "approved" in payment_statuses:
        provider_status, provider_reference = "CONFIRMED", str(payment_id)
        approved = next((item for item in merchant_order.get("payments", []) if item.get("status") == "approved"), {})
        provider_reference = str(approved.get("id") or payment_id)
    elif payment_statuses and all(status_name in {"cancelled", "refunded", "charged_back", "rejected"} for status_name in payment_statuses):
        provider_status, provider_reference = "DECLINED", str(payment_id)
    else:
        return {"received": True}

    if payment.status in {"REFUND_REQUIRED", "CONFIRMED", "DECLINED"}:
        return {"received": True}
    payment.status = provider_status
    payment.provider = "MERCADO_PAGO"
    payment.provider_reference = provider_reference
    payment.verified_at = datetime.now(timezone.utc)
    db.commit()

    quote = db.get(Quote, payment.quote_id)
    if messaging.MESSAGING_ENABLED:
        event_name = "PaymentConfirmed" if provider_status == "CONFIRMED" else "PaymentFailed"
        emit_os_event(
            event_name,
            {"order_id": quote.order_id, "payment_id": payment.id},
            order_id=quote.order_id,
            dedup_id=f"mp-{event_name}-{payment.id}",
        )
        db.refresh(payment)
        return {"received": True}

    if provider_status == "CONFIRMED":
        try:
            response = httpx.post(
                f"{OS_SERVICE_URL}/internal/payments/confirmed",
                json={"order_id": quote.order_id, "payment_id": payment.id},
                headers={"X-Internal-Token": INTERNAL_TOKEN},
                timeout=5,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.exception("Mercado Pago payment %s verified; OS saga notification failed", payment.id)
            raise HTTPException(status_code=503, detail="payment verified; saga notification can be retried") from exc
        db.refresh(payment)
    return {"received": True}


@app.post("/internal/payments/{payment_id}/compensate")
def request_compensation(
    payment_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_internal_token),
):
    payment = db.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="payment not found")
    if payment.status == "DECLINED":
        raise HTTPException(status_code=409, detail="declined payment does not require refund")
    payment.status = "REFUND_REQUIRED"
    db.commit()
    return {"payment_id": payment.id, "status": payment.status}
