"""Authorized Service Centre organization (simulated).

Owns: technician capacity, appointment slots, diagnosis and repair progress.

API style: `X-Partner-Key` header auth, `{"result": "...", "data": {...}}`
envelope, mandatory `Idempotency-Key` on booking creation.

Bookings are the second real side effect in the system (the first being part
reservation), so the same idempotency discipline applies: a replayed booking
returns the original appointment rather than consuming a second slot.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, Integer, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from providers.common.sim import (
    FailureSimulator,
    build_sim_router,
    install_invalid_response_handler,
)
from providers.common.storage import ProviderStore

SERVICE_NAME = "service_centre"
PARTNER_KEY = os.getenv("SERVICE_CENTRE_KEY", "svc-dev-key")

store = ProviderStore(SERVICE_NAME)
simulator = FailureSimulator(SERVICE_NAME)
Base = store.Base

VALID_STATUSES = (
    "SCHEDULED", "CHECKED_IN", "ACCEPTED", "REJECTED", "DIAGNOSING", "AWAITING_PART",
    "REPAIRING", "COMPLETED", "VERIFIED", "CANCELLED", "FAILED",
)


class ServiceCentreBranch(Base):
    __tablename__ = "svc_branches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    partner_code: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    city: Mapped[str] = mapped_column(String(128), default="Pune")
    region: Mapped[str] = mapped_column(String(8), default="IN")
    daily_capacity: Mapped[int] = mapped_column(Integer, default=8)
    booked_today: Mapped[int] = mapped_column(Integer, default=0)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)


class ServiceBooking(Base):
    __tablename__ = "svc_bookings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    booking_ref: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(80), unique=True, nullable=False, index=True
    )
    partner_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    serial_number: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    issue_type: Mapped[str] = mapped_column(String(64), nullable=False)
    component_sku: Mapped[str | None] = mapped_column(String(64))
    external_ref: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(24), default="SCHEDULED", nullable=False)
    technician: Mapped[str | None] = mapped_column(String(128))
    diagnosis_notes: Mapped[str | None] = mapped_column(String(1024))
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


def require_partner_key(x_partner_key: str | None = Header(default=None)) -> None:
    if x_partner_key != PARTNER_KEY:
        raise HTTPException(
            status_code=401,
            detail={"result": "error", "code": "AUTH", "message": "invalid X-Partner-Key"},
        )


app = FastAPI(
    title="Authorized Service Centre API (simulated)",
    version="3.0.0",
    description="Independent service centre organization owning appointments and repairs.",
)
install_invalid_response_handler(app)
app.include_router(build_sim_router(simulator))


@app.on_event("startup")
def _startup() -> None:
    store.create_all()


@app.get("/health", tags=["ops"])
def health() -> dict:
    return {"service": SERVICE_NAME, "status": "UP", "time": datetime.now(UTC)}


@app.get("/v3/capacity/{partner_code}", tags=["capacity"],
         dependencies=[Depends(require_partner_key)])
async def capacity(partner_code: str, db: Session = Depends(store.dependency)) -> dict:
    await simulator.before("capacity.get")
    branch = db.scalar(
        select(ServiceCentreBranch).where(ServiceCentreBranch.partner_code == partner_code)
    )
    if branch is None:
        raise HTTPException(
            status_code=404, detail={"result": "error", "message": "unknown partner"}
        )
    return {
        "result": "ok",
        "data": {
            "partner_code": branch.partner_code,
            "name": branch.name,
            "city": branch.city,
            "region": branch.region,
            "is_open": branch.is_open,
            "daily_capacity": branch.daily_capacity,
            "booked_today": branch.booked_today,
            "available_slots": max(0, branch.daily_capacity - branch.booked_today),
        },
    }


class BookingRequest(BaseModel):
    partner_code: str
    serial_number: str = Field(..., max_length=64)
    issue_type: str = Field(..., max_length=64)
    component_sku: str | None = None
    external_ref: str | None = Field(None, max_length=64)
    preferred_date: datetime | None = None


@app.post("/v3/bookings", tags=["bookings"], dependencies=[Depends(require_partner_key)])
async def create_booking(
    req: BookingRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: Session = Depends(store.dependency),
) -> dict:
    await simulator.before("bookings.create")

    existing = db.scalar(
        select(ServiceBooking).where(ServiceBooking.idempotency_key == idempotency_key)
    )
    if existing is not None:
        await simulator.after_commit("bookings.create")
        return {"result": "ok", "data": _booking_payload(existing, replayed=True)}

    branch = db.scalar(
        select(ServiceCentreBranch).where(
            ServiceCentreBranch.partner_code == req.partner_code
        )
    )
    if branch is None:
        raise HTTPException(
            status_code=404,
            detail={"result": "error", "code": "NO_PARTNER", "message": "unknown partner"},
        )
    if not branch.is_open:
        raise HTTPException(
            status_code=409,
            detail={"result": "error", "code": "CLOSED", "message": "branch is closed"},
        )
    if branch.booked_today >= branch.daily_capacity:
        raise HTTPException(
            status_code=409,
            detail={
                "result": "error",
                "code": "NO_CAPACITY",
                "message": "no slots available today",
            },
        )

    scheduled = req.preferred_date or (datetime.now(UTC) + timedelta(days=1))
    booking = ServiceBooking(
        id=str(uuid.uuid4()),
        booking_ref=f"BK-{uuid.uuid4().hex[:10].upper()}",
        idempotency_key=idempotency_key,
        partner_code=req.partner_code,
        serial_number=req.serial_number,
        issue_type=req.issue_type,
        component_sku=req.component_sku,
        external_ref=req.external_ref,
        scheduled_for=scheduled,
        technician=f"TECH-{uuid.uuid4().hex[:4].upper()}",
    )
    branch.booked_today += 1
    db.add(booking)
    db.flush()
    db.commit()

    await simulator.after_commit("bookings.create")
    return {"result": "ok", "data": _booking_payload(booking, replayed=False)}


def _booking_payload(b: ServiceBooking, replayed: bool) -> dict:
    return {
        "booking_ref": b.booking_ref,
        "idempotency_key": b.idempotency_key,
        "partner_code": b.partner_code,
        "serial_number": b.serial_number,
        "issue_type": b.issue_type,
        "component_sku": b.component_sku,
        "external_ref": b.external_ref,
        "status": b.status,
        "technician": b.technician,
        "diagnosis_notes": b.diagnosis_notes,
        "scheduled_for": b.scheduled_for.isoformat() if b.scheduled_for else None,
        "completed_at": b.completed_at.isoformat() if b.completed_at else None,
        "replayed": replayed,
    }


@app.get("/v3/bookings/by-key/{idempotency_key}", tags=["bookings"],
         dependencies=[Depends(require_partner_key)])
async def booking_by_key(
    idempotency_key: str, db: Session = Depends(store.dependency)
) -> dict:
    """Reconciliation endpoint for unknown booking outcomes."""
    await simulator.before("bookings.by_key")
    b = db.scalar(
        select(ServiceBooking).where(ServiceBooking.idempotency_key == idempotency_key)
    )
    if b is None:
        return {"result": "ok", "data": {"found": False, "idempotency_key": idempotency_key}}
    return {"result": "ok", "data": {**_booking_payload(b, True), "found": True}}


class StatusUpdate(BaseModel):
    status: str
    technician: str | None = None
    notes: str | None = None


@app.patch("/v3/bookings/{booking_ref}/status", tags=["bookings"],
           dependencies=[Depends(require_partner_key)])
async def update_status(
    booking_ref: str, req: StatusUpdate, db: Session = Depends(store.dependency)
) -> dict:
    """Used by the Provider Portal to advance a repair."""
    await simulator.before("bookings.update")

    if req.status not in VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail={
                "result": "error",
                "code": "BAD_STATUS",
                "message": f"status must be one of {list(VALID_STATUSES)}",
            },
        )
    b = db.scalar(select(ServiceBooking).where(ServiceBooking.booking_ref == booking_ref))
    if b is None:
        raise HTTPException(
            status_code=404, detail={"result": "error", "message": "booking not found"}
        )

    b.status = req.status
    if req.technician:
        b.technician = req.technician
    if req.notes:
        b.diagnosis_notes = req.notes
    if req.status == "COMPLETED":
        b.completed_at = datetime.now(UTC)
    db.flush()
    return {"result": "ok", "data": _booking_payload(b, replayed=False)}


@app.get("/v3/bookings/{booking_ref}", tags=["bookings"],
         dependencies=[Depends(require_partner_key)])
async def get_booking(booking_ref: str, db: Session = Depends(store.dependency)) -> dict:
    await simulator.before("bookings.get")
    b = db.scalar(select(ServiceBooking).where(ServiceBooking.booking_ref == booking_ref))
    if b is None:
        raise HTTPException(
            status_code=404, detail={"result": "error", "message": "booking not found"}
        )
    return {"result": "ok", "data": _booking_payload(b, replayed=False)}


@app.post("/v3/bookings/{booking_ref}/cancel", tags=["bookings"],
          dependencies=[Depends(require_partner_key)])
async def cancel_booking(
    booking_ref: str,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: Session = Depends(store.dependency),
) -> dict:
    """Compensating action for a booking."""
    await simulator.before("bookings.cancel")
    b = db.scalar(select(ServiceBooking).where(ServiceBooking.booking_ref == booking_ref))
    if b is None:
        raise HTTPException(
            status_code=404, detail={"result": "error", "message": "booking not found"}
        )
    if b.status == "CANCELLED":
        return {"result": "ok", "data": {**_booking_payload(b, True), "already_cancelled": True}}

    branch = db.scalar(
        select(ServiceCentreBranch).where(
            ServiceCentreBranch.partner_code == b.partner_code
        )
    )
    if branch is not None:
        branch.booked_today = max(0, branch.booked_today - 1)
    b.status = "CANCELLED"
    db.flush()
    return {
        "result": "ok",
        "data": {**_booking_payload(b, False), "already_cancelled": False},
    }


@app.get("/v3/bookings", tags=["bookings"], dependencies=[Depends(require_partner_key)])
async def list_bookings(
    partner_code: str | None = None, db: Session = Depends(store.dependency)
) -> dict:
    stmt = select(ServiceBooking)
    if partner_code:
        stmt = stmt.where(ServiceBooking.partner_code == partner_code)
    rows = list(db.scalars(stmt))
    return {"result": "ok", "data": [_booking_payload(b, False) for b in rows]}
