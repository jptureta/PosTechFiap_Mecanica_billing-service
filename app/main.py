import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, Numeric, String, create_engine, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.auth import Actor, get_actor

logger = logging.getLogger(__name__)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./billing-service.db")
OS_SERVICE_URL = os.getenv("OS_SERVICE_URL", "http://os-service:8000").rstrip("/")
INTERNAL_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "")
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "")
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


app = FastAPI(title="Orçamento e Pagamento", version="1.0.0")


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
    if payload.status == "CONFIRMED":
        quote = db.get(Quote, payment.quote_id)
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
