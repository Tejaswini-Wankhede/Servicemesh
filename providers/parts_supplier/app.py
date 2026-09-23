"""Parts Supplier organization (simulated).

Owns: parts inventory, stock levels and reservations.

API style: API key as a *query parameter* (a genuinely common legacy pattern),
`{"ok": true, "data": {...}}` response envelope, and a mandatory
`Idempotency-Key` header on every reservation.

This service carries the heaviest correctness burden in the demo because
`POST /reservations` is a real side effect: reserving twice consumes two units
of stock. Two mechanisms protect against that:

1. A unique index on `idempotency_key`. A replayed request returns the
   *original* reservation with `replayed: true` instead of creating a new one.
2. `GET /reservations/by-key/{key}`, which lets a caller that timed out and
   does not know what happened ask "did my reservation actually land?" - this
   is how ServiceMesh resolves an UNKNOWN outcome without guessing.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, Float, Integer, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from providers.common.sim import (
    FailureSimulator,
    build_sim_router,
    install_invalid_response_handler,
)
from providers.common.storage import ProviderStore

SERVICE_NAME = "parts_supplier"
API_KEY = os.getenv("SUPPLIER_API_KEY", "sup-dev-key")

store = ProviderStore(SERVICE_NAME)
simulator = FailureSimulator(SERVICE_NAME)
Base = store.Base


class SupplierStock(Base):
    __tablename__ = "sup_stock"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    supplier_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sku: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(255), default="")
    on_hand: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reserved: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, default=0.0)
    lead_time_days: Mapped[int] = mapped_column(Integer, default=2)
    warehouse_region: Mapped[str] = mapped_column(String(8), default="IN")

    @property
    def available(self) -> int:
        return max(0, self.on_hand - self.reserved)


class SupplierReservation(Base):
    __tablename__ = "sup_reservations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    reservation_ref: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    #: THE idempotency guarantee. Unique index => a replay cannot double-book.
    idempotency_key: Mapped[str] = mapped_column(
        String(80), unique=True, nullable=False, index=True
    )
    supplier_code: Mapped[str] = mapped_column(String(64), nullable=False)
    sku: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="RESERVED", nullable=False)
    external_ref: Mapped[str | None] = mapped_column(String(64))
    released: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC) + timedelta(days=7),
    )


def require_api_key(api_key: str = Query(..., description="Supplier API key")) -> None:
    if api_key != API_KEY:
        raise HTTPException(
            status_code=401, detail={"ok": False, "error": "invalid api_key"}
        )


app = FastAPI(
    title="Parts Supplier API (simulated)",
    version="3.2.0",
    description="Independent supplier organization owning inventory and reservations.",
)
install_invalid_response_handler(app)
app.include_router(build_sim_router(simulator))


@app.on_event("startup")
def _startup() -> None:
    store.create_all()


@app.get("/health", tags=["ops"])
def health() -> dict:
    return {"service": SERVICE_NAME, "status": "UP", "time": datetime.now(UTC)}


@app.get("/inventory", tags=["inventory"], dependencies=[Depends(require_api_key)])
async def check_inventory(
    sku: str = Query(...),
    quantity: int = Query(1, ge=1),
    supplier_code: str | None = Query(None),
    db: Session = Depends(store.dependency),
) -> dict:
    await simulator.before("inventory.check")

    stmt = select(SupplierStock).where(SupplierStock.sku == sku)
    if supplier_code:
        stmt = stmt.where(SupplierStock.supplier_code == supplier_code)
    rows = list(db.scalars(stmt))
    if not rows:
        return {
            "ok": True,
            "data": {
                "sku": sku,
                "available": False,
                "available_quantity": 0,
                "reason": "sku not carried by this supplier",
            },
        }

    best = max(rows, key=lambda r: r.available)
    return {
        "ok": True,
        "data": {
            "sku": sku,
            "supplier_code": best.supplier_code,
            "available": best.available >= quantity,
            "available_quantity": best.available,
            "requested_quantity": quantity,
            "unit_price": best.unit_price,
            "lead_time_days": best.lead_time_days,
            "warehouse_region": best.warehouse_region,
            "reason": (
                "in stock" if best.available >= quantity
                else f"only {best.available} available"
            ),
        },
    }


class ReservationRequest(BaseModel):
    sku: str = Field(..., max_length=64)
    quantity: int = Field(1, ge=1, le=10)
    supplier_code: str | None = None
    external_ref: str | None = Field(None, max_length=64)


@app.post("/reservations", tags=["reservations"], dependencies=[Depends(require_api_key)])
async def create_reservation(
    req: ReservationRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: Session = Depends(store.dependency),
) -> dict:
    """Reserve stock. Safe to call repeatedly with the same Idempotency-Key."""
    await simulator.before("reservations.create")

    # --- replay check BEFORE any mutation -------------------------------
    existing = db.scalar(
        select(SupplierReservation).where(
            SupplierReservation.idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        await simulator.after_commit("reservations.create")
        return {
            "ok": True,
            "data": _reservation_payload(existing, replayed=True),
        }

    stmt = select(SupplierStock).where(SupplierStock.sku == req.sku)
    if req.supplier_code:
        stmt = stmt.where(SupplierStock.supplier_code == req.supplier_code)
    rows = list(db.scalars(stmt))
    if not rows:
        raise HTTPException(
            status_code=404,
            detail={"ok": False, "error": "sku_not_found", "sku": req.sku},
        )

    stock = max(rows, key=lambda r: r.available)
    if stock.available < req.quantity:
        raise HTTPException(
            status_code=409,
            detail={
                "ok": False,
                "error": "out_of_stock",
                "sku": req.sku,
                "available_quantity": stock.available,
                "requested_quantity": req.quantity,
            },
        )

    reservation = SupplierReservation(
        id=str(uuid.uuid4()),
        reservation_ref=f"RES-{uuid.uuid4().hex[:10].upper()}",
        idempotency_key=idempotency_key,
        supplier_code=stock.supplier_code,
        sku=req.sku,
        quantity=req.quantity,
        external_ref=req.external_ref,
    )
    stock.reserved += req.quantity
    db.add(reservation)
    db.flush()
    db.commit()  # commit before any simulated post-commit timeout

    # This may hang/504 *after* the reservation is durable -> caller sees an
    # UNKNOWN outcome and must reconcile via /reservations/by-key.
    await simulator.after_commit("reservations.create")

    return {"ok": True, "data": _reservation_payload(reservation, replayed=False)}


def _reservation_payload(r: SupplierReservation, replayed: bool) -> dict:
    return {
        "reservation_ref": r.reservation_ref,
        "idempotency_key": r.idempotency_key,
        "supplier_code": r.supplier_code,
        "sku": r.sku,
        "quantity": r.quantity,
        "status": r.status,
        "external_ref": r.external_ref,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "expires_at": r.expires_at.isoformat() if r.expires_at else None,
        "replayed": replayed,
    }



class DispatchRequest(BaseModel):
    eta: str | None = Field(None, max_length=64)


@app.post("/reservations/{reservation_ref}/dispatch", tags=["reservations"],
          dependencies=[Depends(require_api_key)])
async def dispatch_reservation(
    reservation_ref: str, req: DispatchRequest, db: Session = Depends(store.dependency)
) -> dict:
    """Supplier portal operation: dispatch a previously reserved part."""
    await simulator.before("reservations.dispatch")
    reservation = db.scalar(select(SupplierReservation).where(
        SupplierReservation.reservation_ref == reservation_ref
    ))
    if reservation is None:
        raise HTTPException(status_code=404, detail={"ok": False, "error": "reservation_not_found"})
    if reservation.released:
        raise HTTPException(status_code=409, detail={"ok": False, "error": "reservation_released"})
    reservation.status = "DISPATCHED"
    db.flush()
    payload = _reservation_payload(reservation, replayed=False)
    payload["dispatch_status"] = "DISPATCHED"
    payload["eta"] = req.eta
    return {"ok": True, "data": payload}

@app.get("/reservations/by-key/{idempotency_key}", tags=["reservations"],
         dependencies=[Depends(require_api_key)])
async def reservation_by_key(
    idempotency_key: str, db: Session = Depends(store.dependency)
) -> dict:
    """Reconciliation endpoint: 'did my reservation actually happen?'

    Deliberately NOT gated by the failure simulator for the reservation
    operation, because an operator reconciling an unknown outcome needs a
    dependable answer. It has its own simulation key so it can still be failed
    independently if a test wants to.
    """
    await simulator.before("reservations.by_key")

    r = db.scalar(
        select(SupplierReservation).where(
            SupplierReservation.idempotency_key == idempotency_key
        )
    )
    if r is None:
        return {"ok": True, "data": {"found": False, "idempotency_key": idempotency_key}}
    payload = _reservation_payload(r, replayed=True)
    payload["found"] = True
    return {"ok": True, "data": payload}


@app.post("/reservations/{reservation_ref}/release", tags=["reservations"],
          dependencies=[Depends(require_api_key)])
async def release_reservation(
    reservation_ref: str,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: Session = Depends(store.dependency),
) -> dict:
    """Compensating action for a reservation (returns stock to the pool)."""
    await simulator.before("reservations.release")

    r = db.scalar(
        select(SupplierReservation).where(
            SupplierReservation.reservation_ref == reservation_ref
        )
    )
    if r is None:
        raise HTTPException(
            status_code=404, detail={"ok": False, "error": "reservation_not_found"}
        )
    if r.released:
        return {"ok": True, "data": {**_reservation_payload(r, True), "already_released": True}}

    stock = db.scalar(
        select(SupplierStock).where(
            SupplierStock.sku == r.sku, SupplierStock.supplier_code == r.supplier_code
        )
    )
    if stock is not None:
        stock.reserved = max(0, stock.reserved - r.quantity)
    r.released = True
    r.status = "RELEASED"
    db.flush()
    return {"ok": True, "data": {**_reservation_payload(r, False), "already_released": False}}


@app.get("/stock", tags=["inventory"], dependencies=[Depends(require_api_key)])
async def list_stock(db: Session = Depends(store.dependency)) -> dict:
    """Operational view used by the supplier's own portal page."""
    rows = list(db.scalars(select(SupplierStock)))
    return {
        "ok": True,
        "data": [
            {
                "supplier_code": s.supplier_code,
                "sku": s.sku,
                "description": s.description,
                "on_hand": s.on_hand,
                "reserved": s.reserved,
                "available": s.available,
                "unit_price": s.unit_price,
                "lead_time_days": s.lead_time_days,
            }
            for s in rows
        ],
    }
