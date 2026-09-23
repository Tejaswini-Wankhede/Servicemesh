"""Manufacturer / OEM organization (simulated).

Owns: the serial-number registry, model definitions, the approved component
list per model, and whether a service centre is an *authorized* partner.

API style: deliberately different from the marketplace - PascalCase fields
inside a `{"Status":..., "Data":...}` envelope, Bearer-token auth. ServiceMesh
must normalise this; the orchestrator never sees `SerialNo`.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from providers.common.sim import (
    FailureSimulator,
    build_sim_router,
    install_invalid_response_handler,
)
from providers.common.storage import ProviderStore

SERVICE_NAME = "manufacturer"
BEARER_TOKEN = os.getenv("MANUFACTURER_TOKEN", "oem-dev-token")

store = ProviderStore(SERVICE_NAME)
simulator = FailureSimulator(SERVICE_NAME)
Base = store.Base


class OemModel(Base):
    __tablename__ = "oem_models"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    model_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    manufacturer_code: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(64), default="LAPTOP")
    release_year: Mapped[int | None] = mapped_column(Integer)
    standard_warranty_months: Mapped[int] = mapped_column(Integer, default=12)
    #: SKUs the OEM certifies for this model. Authoritative upstream input to
    #: ServiceMesh's compatibility engine (but not the final decision-maker).
    approved_component_skus: Mapped[list] = mapped_column(JSON, default=list)

    units: Mapped[list[OemUnit]] = relationship(back_populates="model")


class OemUnit(Base):
    """One physical manufactured unit, identified by serial number."""

    __tablename__ = "oem_units"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    serial_number: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    model_id: Mapped[str] = mapped_column(String(36), ForeignKey("oem_models.id"))
    manufactured_on: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    plant_code: Mapped[str] = mapped_column(String(32), default="PLANT-A")
    is_recalled: Mapped[bool] = mapped_column(Boolean, default=False)
    bios_version: Mapped[str] = mapped_column(String(16), default="1.6")

    model: Mapped[OemModel] = relationship(back_populates="units")


class OemAuthorizedPartner(Base):
    """Which service centres this OEM authorizes, and for which models."""

    __tablename__ = "oem_authorized_partners"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    partner_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    partner_name: Mapped[str] = mapped_column(String(255), nullable=False)
    region: Mapped[str] = mapped_column(String(8), default="IN")
    authorization_level: Mapped[str] = mapped_column(String(24), default="FULL")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    authorized_categories: Mapped[list] = mapped_column(JSON, default=list)


def require_bearer(authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {BEARER_TOKEN}"
    if authorization != expected:
        raise HTTPException(
            status_code=401,
            detail={"Status": "ERROR", "Code": "AUTH-401", "Message": "invalid bearer token"},
        )


app = FastAPI(
    title="Manufacturer OEM API (simulated)",
    version="2.0.0",
    description="Independent OEM organization owning serial registry and partner authorization.",
)
install_invalid_response_handler(app)
app.include_router(build_sim_router(simulator))


@app.on_event("startup")
def _startup() -> None:
    store.create_all()


@app.get("/health", tags=["ops"])
def health() -> dict:
    return {"service": SERVICE_NAME, "status": "UP", "time": datetime.now(UTC)}


@app.get("/oem/v2/registry/{serial_no}", tags=["registry"],
         dependencies=[Depends(require_bearer)])
async def registry_lookup(serial_no: str, db: Session = Depends(store.dependency)) -> dict:
    """Serial-number verification. Note the PascalCase envelope."""
    await simulator.before("registry.lookup")

    unit = db.scalar(select(OemUnit).where(OemUnit.serial_number == serial_no))
    if unit is None:
        raise HTTPException(
            status_code=404,
            detail={
                "Status": "ERROR",
                "Code": "OEM-404",
                "Message": f"serial {serial_no} not found in registry",
            },
        )
    model = unit.model
    return {
        "Status": "SUCCESS",
        "Code": "OEM-000",
        "Data": {
            "SerialNo": unit.serial_number,
            "ModelCode": model.model_code,
            "ModelName": model.model_name,
            "ManufacturerCode": model.manufacturer_code,
            "Category": model.category,
            "ManufacturedOn": unit.manufactured_on.isoformat(),
            "PlantCode": unit.plant_code,
            "BiosVersion": unit.bios_version,
            "IsRecalled": unit.is_recalled,
            "StandardWarrantyMonths": model.standard_warranty_months,
            "ApprovedComponentSkus": model.approved_component_skus,
        },
    }


@app.get("/oem/v2/models/{model_code}/components", tags=["catalogue"],
         dependencies=[Depends(require_bearer)])
async def model_components(model_code: str, db: Session = Depends(store.dependency)) -> dict:
    await simulator.before("catalogue.components")
    model = db.scalar(select(OemModel).where(OemModel.model_code == model_code))
    if model is None:
        raise HTTPException(
            status_code=404,
            detail={"Status": "ERROR", "Code": "OEM-404", "Message": "unknown model"},
        )
    return {
        "Status": "SUCCESS",
        "Code": "OEM-000",
        "Data": {
            "ModelCode": model.model_code,
            "ApprovedComponentSkus": model.approved_component_skus,
        },
    }


class PartnerCheckRequest(BaseModel):
    PartnerCode: str
    Category: str = "LAPTOP"
    Region: str = "IN"


@app.post("/oem/v2/partners/verify", tags=["authorization"],
          dependencies=[Depends(require_bearer)])
async def verify_partner(
    req: PartnerCheckRequest, db: Session = Depends(store.dependency)
) -> dict:
    """Is this service centre authorized by the OEM for this category/region?

    A negative answer here is a *business rejection*, not a failure: retrying
    will never change it. ServiceMesh must pick a different provider instead.
    """
    await simulator.before("partners.verify")

    partner = db.scalar(
        select(OemAuthorizedPartner).where(
            OemAuthorizedPartner.partner_code == req.PartnerCode
        )
    )
    if partner is None or not partner.is_active:
        return {
            "Status": "SUCCESS",
            "Code": "OEM-000",
            "Data": {
                "PartnerCode": req.PartnerCode,
                "Authorized": False,
                "Reason": "partner not present in OEM authorization registry",
            },
        }

    authorized = (
        req.Category in (partner.authorized_categories or [])
        and partner.region == req.Region
    )
    reason = "authorized"
    if req.Category not in (partner.authorized_categories or []):
        reason = f"partner not authorized for category {req.Category}"
    elif partner.region != req.Region:
        reason = f"partner operates in {partner.region}, not {req.Region}"

    return {
        "Status": "SUCCESS",
        "Code": "OEM-000",
        "Data": {
            "PartnerCode": partner.partner_code,
            "PartnerName": partner.partner_name,
            "Authorized": authorized,
            "AuthorizationLevel": partner.authorization_level,
            "Reason": reason,
        },
    }
