"""Marketplace organization (simulated).

Owns: orders, order items, the customer's purchase record.
API style: plain snake_case REST, API key in `X-API-Key`.
Error style: RFC-ish `{"error": ..., "message": ...}`.

This service knows nothing about warranties, parts or repairs. It cannot tell
you whether a laptop is under warranty - only that it was sold, when, and to
whom. That limitation is the point.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Float, ForeignKey, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from providers.common.sim import (
    FailureSimulator,
    build_sim_router,
    install_invalid_response_handler,
)
from providers.common.storage import ProviderStore

SERVICE_NAME = "marketplace"
API_KEY = os.getenv("MARKETPLACE_API_KEY", "mkt-dev-key")

store = ProviderStore(SERVICE_NAME)
simulator = FailureSimulator(SERVICE_NAME)
Base = store.Base


# --------------------------------------------------------------------------
# Marketplace's own schema (independent of ServiceMesh)
# --------------------------------------------------------------------------


class MarketplaceCustomer(Base):
    __tablename__ = "mkt_customers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    city: Mapped[str | None] = mapped_column(String(128))
    region: Mapped[str] = mapped_column(String(8), default="IN")

    orders: Mapped[list[MarketplaceOrder]] = relationship(back_populates="customer")


class MarketplaceOrder(Base):
    __tablename__ = "mkt_orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_reference: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    customer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mkt_customers.id"), nullable=False
    )
    purchased_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="DELIVERED", nullable=False)
    channel: Mapped[str] = mapped_column(String(24), default="ONLINE", nullable=False)
    total_amount: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), default="INR")

    customer: Mapped[MarketplaceCustomer] = relationship(back_populates="orders")
    items: Mapped[list[MarketplaceOrderItem]] = relationship(back_populates="order")


class MarketplaceOrderItem(Base):
    __tablename__ = "mkt_order_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mkt_orders.id"), nullable=False
    )
    model_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    serial_number: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    seller_code: Mapped[str] = mapped_column(String(64), default="SELLER-001")
    unit_price: Mapped[float] = mapped_column(Float, default=0.0)

    order: Mapped[MarketplaceOrder] = relationship(back_populates="items")


# --------------------------------------------------------------------------
# API contract (marketplace-specific shapes)
# --------------------------------------------------------------------------


class OrderLookupRequest(BaseModel):
    order_reference: str = Field(..., min_length=3, max_length=64)
    serial_number: str | None = Field(None, max_length=64)


class OrderItemOut(BaseModel):
    model_code: str
    product_name: str
    serial_number: str
    seller_code: str
    unit_price: float


class OrderLookupResponse(BaseModel):
    order_reference: str
    order_status: str
    purchased_at: datetime
    channel: str
    total_amount: float
    currency: str
    buyer_email: str
    buyer_name: str
    buyer_city: str | None
    buyer_region: str
    items: list[OrderItemOut]
    verified: bool
    verification_note: str


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if x_api_key != API_KEY:
        raise HTTPException(
            status_code=401, detail={"error": "unauthorized", "message": "invalid X-API-Key"}
        )


app = FastAPI(
    title="Marketplace API (simulated)",
    version="1.0.0",
    description="Independent marketplace organization owning order and purchase records.",
)
install_invalid_response_handler(app)
app.include_router(build_sim_router(simulator))


@app.on_event("startup")
def _startup() -> None:
    store.create_all()


@app.get("/health", tags=["ops"])
def health() -> dict:
    return {"service": SERVICE_NAME, "status": "UP", "time": datetime.now(UTC)}


@app.post(
    "/api/v1/orders/lookup",
    response_model=OrderLookupResponse,
    tags=["orders"],
    dependencies=[Depends(require_api_key)],
)
async def lookup_order(
    req: OrderLookupRequest, db: Session = Depends(store.dependency)
) -> OrderLookupResponse:
    """Verify that an order exists and (optionally) contains a given serial."""
    await simulator.before("orders.lookup")

    order = db.scalar(
        select(MarketplaceOrder).where(MarketplaceOrder.order_reference == req.order_reference)
    )
    if order is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "order_not_found", "message": f"no order {req.order_reference}"},
        )

    items = list(order.items)
    if req.serial_number:
        matching = [i for i in items if i.serial_number == req.serial_number]
        if not matching:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "serial_not_in_order",
                    "message": (
                        f"serial {req.serial_number} is not part of order "
                        f"{req.order_reference}"
                    ),
                },
            )
        items = matching

    return OrderLookupResponse(
        order_reference=order.order_reference,
        order_status=order.status,
        purchased_at=order.purchased_at,
        channel=order.channel,
        total_amount=order.total_amount,
        currency=order.currency,
        buyer_email=order.customer.email,
        buyer_name=order.customer.full_name,
        buyer_city=order.customer.city,
        buyer_region=order.customer.region,
        items=[
            OrderItemOut(
                model_code=i.model_code,
                product_name=i.product_name,
                serial_number=i.serial_number,
                seller_code=i.seller_code,
                unit_price=i.unit_price,
            )
            for i in items
        ],
        verified=order.status in {"DELIVERED", "COMPLETED"},
        verification_note=(
            "purchase confirmed" if order.status in {"DELIVERED", "COMPLETED"}
            else f"order is in state {order.status}"
        ),
    )


@app.get("/api/v1/orders/{order_reference}", tags=["orders"],
         dependencies=[Depends(require_api_key)])
async def get_order(order_reference: str, db: Session = Depends(store.dependency)) -> dict:
    await simulator.before("orders.get")
    order = db.scalar(
        select(MarketplaceOrder).where(MarketplaceOrder.order_reference == order_reference)
    )
    if order is None:
        raise HTTPException(
            status_code=404, detail={"error": "order_not_found", "message": order_reference}
        )
    return {
        "order_reference": order.order_reference,
        "order_status": order.status,
        "purchased_at": order.purchased_at.isoformat(),
        "buyer_email": order.customer.email,
        "items": [
            {"model_code": i.model_code, "serial_number": i.serial_number} for i in order.items
        ],
    }
