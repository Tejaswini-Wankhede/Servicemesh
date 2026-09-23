"""Warranty Provider organization (simulated).

Owns: warranty contracts, coverage terms, exclusions and claim authorization.

API style: a legacy SOAP-flavoured JSON envelope (`Envelope/Body/...Result`)
with numeric status codes, and two-factor header auth (client id + secret).
This is the most awkward contract in the system on purpose - it is the one that
best justifies an adapter layer.

Key domain point: this organization is the *only* one that can say whether a
repair is covered. An expired contract is a permanent business rejection, and
ServiceMesh must terminate the transaction cleanly rather than retry.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import JSON, Boolean, DateTime, Float, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from providers.common.sim import (
    FailureSimulator,
    build_sim_router,
    install_invalid_response_handler,
)
from providers.common.storage import ProviderStore

SERVICE_NAME = "warranty"
CLIENT_ID = os.getenv("WARRANTY_CLIENT_ID", "wty-client")
CLIENT_SECRET = os.getenv("WARRANTY_CLIENT_SECRET", "wty-secret")

store = ProviderStore(SERVICE_NAME)
simulator = FailureSimulator(SERVICE_NAME)
Base = store.Base

# Warranty provider status codes (its own vocabulary, not HTTP)
WTY_OK = "WTY-000"
WTY_NO_CONTRACT = "WTY-101"
WTY_EXPIRED = "WTY-104"
WTY_VOID = "WTY-107"
WTY_EXCLUDED = "WTY-201"


class WarrantyContract(Base):
    __tablename__ = "wty_contracts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    contract_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    serial_number: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    model_code: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_code: Mapped[str] = mapped_column(String(32), default="STANDARD")
    starts_on: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_on: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_void: Mapped[bool] = mapped_column(Boolean, default=False)
    void_reason: Mapped[str | None] = mapped_column(String(255))
    region: Mapped[str] = mapped_column(String(8), default="IN")
    #: component types this plan covers, e.g. ["BATTERY","SCREEN"]
    covered_component_types: Mapped[list] = mapped_column(JSON, default=list)
    #: issue types explicitly excluded, e.g. ["PHYSICAL_DAMAGE","LIQUID_DAMAGE"]
    excluded_issue_types: Mapped[list] = mapped_column(JSON, default=list)
    coverage_limit: Mapped[float] = mapped_column(Float, default=50000.0)
    deductible: Mapped[float] = mapped_column(Float, default=0.0)
    claims_used: Mapped[int] = mapped_column(default=0)
    max_claims: Mapped[int] = mapped_column(default=3)


def require_client_auth(
    x_client_id: str | None = Header(default=None),
    x_client_secret: str | None = Header(default=None),
) -> None:
    if x_client_id != CLIENT_ID or x_client_secret != CLIENT_SECRET:
        raise HTTPException(
            status_code=401,
            detail={
                "Envelope": {
                    "Body": {
                        "Fault": {
                            "FaultCode": "AUTH-401",
                            "FaultString": "invalid client credentials",
                        }
                    }
                }
            },
        )


def envelope(result_name: str, body: dict, code: str = WTY_OK) -> dict:
    return {
        "Envelope": {
            "Header": {
                "StatusCode": code,
                "Timestamp": datetime.now(UTC).isoformat(),
                "Service": "WarrantyProviderService",
            },
            "Body": {result_name: body},
        }
    }


app = FastAPI(
    title="Warranty Provider API (simulated)",
    version="1.4.0",
    description="Independent warranty organization owning contracts and coverage rules.",
)
install_invalid_response_handler(app)
app.include_router(build_sim_router(simulator))


@app.on_event("startup")
def _startup() -> None:
    store.create_all()


@app.get("/health", tags=["ops"])
def health() -> dict:
    return {"service": SERVICE_NAME, "status": "UP", "time": datetime.now(UTC)}


class VerifyContractRequest(BaseModel):
    SerialNumber: str
    PurchaseDate: str | None = None


@app.post("/warranty/soap/verifyContract", tags=["warranty"],
          dependencies=[Depends(require_client_auth)])
async def verify_contract(
    req: VerifyContractRequest, db: Session = Depends(store.dependency)
) -> dict:
    await simulator.before("warranty.verify")

    contract = db.scalar(
        select(WarrantyContract).where(WarrantyContract.serial_number == req.SerialNumber)
    )
    if contract is None:
        return envelope(
            "VerifyContractResult",
            {
                "Valid": False,
                "SerialNumber": req.SerialNumber,
                "Reason": "no warranty contract registered for this serial",
                "Terminal": True,
            },
            code=WTY_NO_CONTRACT,
        )

    now = datetime.now(UTC)
    expires = contract.expires_on
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)

    if contract.is_void:
        return envelope(
            "VerifyContractResult",
            {
                "Valid": False,
                "ContractNo": contract.contract_no,
                "SerialNumber": contract.serial_number,
                "Reason": contract.void_reason or "contract voided",
                "Terminal": True,
            },
            code=WTY_VOID,
        )

    if expires < now:
        return envelope(
            "VerifyContractResult",
            {
                "Valid": False,
                "ContractNo": contract.contract_no,
                "SerialNumber": contract.serial_number,
                "ExpiresOn": expires.isoformat(),
                "Reason": f"warranty expired on {expires.date().isoformat()}",
                "Terminal": True,
            },
            code=WTY_EXPIRED,
        )

    return envelope(
        "VerifyContractResult",
        {
            "Valid": True,
            "ContractNo": contract.contract_no,
            "SerialNumber": contract.serial_number,
            "ModelCode": contract.model_code,
            "PlanCode": contract.plan_code,
            "StartsOn": contract.starts_on.isoformat(),
            "ExpiresOn": expires.isoformat(),
            "Region": contract.region,
            "ClaimsUsed": contract.claims_used,
            "MaxClaims": contract.max_claims,
            "Terminal": False,
        },
    )


class CheckCoverageRequest(BaseModel):
    ContractNo: str
    ComponentType: str
    IssueType: str
    EstimatedCost: float = 0.0
    Region: str = "IN"


@app.post("/warranty/soap/checkCoverage", tags=["warranty"],
          dependencies=[Depends(require_client_auth)])
async def check_coverage(
    req: CheckCoverageRequest, db: Session = Depends(store.dependency)
) -> dict:
    """Decide whether this specific repair is covered under this contract."""
    await simulator.before("warranty.coverage")

    contract = db.scalar(
        select(WarrantyContract).where(WarrantyContract.contract_no == req.ContractNo)
    )
    if contract is None:
        return envelope(
            "CheckCoverageResult",
            {"Covered": False, "Reason": "unknown contract", "Terminal": True},
            code=WTY_NO_CONTRACT,
        )

    reasons: list[str] = []
    covered = True

    if req.IssueType in (contract.excluded_issue_types or []):
        covered = False
        reasons.append(f"issue type {req.IssueType} is excluded by plan {contract.plan_code}")

    if (contract.covered_component_types
            and req.ComponentType not in contract.covered_component_types):
        covered = False
        reasons.append(f"component type {req.ComponentType} not covered by plan")

    if contract.claims_used >= contract.max_claims:
        covered = False
        reasons.append("claim limit exhausted")

    if req.Region != contract.region:
        covered = False
        reasons.append(f"contract valid in {contract.region}, request from {req.Region}")

    approved_amount = 0.0
    if covered:
        approved_amount = max(0.0, min(req.EstimatedCost, contract.coverage_limit)
                              - contract.deductible)

    return envelope(
        "CheckCoverageResult",
        {
            "Covered": covered,
            "ContractNo": contract.contract_no,
            "ComponentType": req.ComponentType,
            "IssueType": req.IssueType,
            "ApprovedAmount": approved_amount,
            "Deductible": contract.deductible,
            "CoverageLimit": contract.coverage_limit,
            "Reason": "; ".join(reasons) if reasons else "covered under plan",
            "Terminal": not covered,
            "AuthorizationCode": (
                f"AUTH-{contract.contract_no[-6:]}" if covered else None
            ),
        },
        code=WTY_OK if covered else WTY_EXCLUDED,
    )
