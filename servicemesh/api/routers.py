"""ServiceMesh REST API routers."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from servicemesh.api import serializers, simulation
from servicemesh.api.deps import (
    CurrentUser,
    audit,
    get_owned_transaction,
    require_admin,
    require_provider_or_admin,
    require_repair_workflow,
)
from servicemesh.api.schemas import (
    CreateTransactionRequest,
    CustomerOut,
    DashboardMetrics,
    DriveResponse,
    LoginRequest,
    NaturalLanguageRequest,
    ProviderJobOut,
    ProviderStats,
    RepairUpdateRequest,
    SimulationRequest,
    SupplierDispatchRequest,
    AdminInterventionRequest,
    TokenResponse,
    TransactionDetail,
    TransactionSummary,
    UserOut,
)
from servicemesh.core.config import get_settings
from servicemesh.core.db import get_db
from servicemesh.core.enums import (
    TERMINAL_STATES,
    OperationStatus,
    OperationType,
    TransactionState,
    UserRole,
)
from servicemesh.core.models import (
    Component,
    Customer,
    FailureRecord,
    Provider,
    ProviderOperation,
    RecoveryAction,
    ServiceTransaction,
    User,
    AuditEvent,
)
from servicemesh.core.security import create_access_token, verify_password
from servicemesh.engines.recovery import describe_matrix
from servicemesh.orchestration.service import (
    advance_repair,
    create_transaction,
    resume_transaction,
    run_transaction,
)

# ===========================================================================
# Auth
# ===========================================================================

auth_router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@auth_router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.scalar(select(User).where(User.email == req.email.lower()))
    if user is None or not verify_password(req.password, user.hashed_password):
        # Identical response whether the email exists or the password is wrong,
        # so the endpoint cannot be used to enumerate accounts.
        audit(db, None, "LOGIN", target=req.email, outcome="FAILURE")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_credentials",
                    "message": "incorrect email or password"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "inactive_account", "message": "account is disabled"},
        )

    settings = get_settings()
    provider_code = user.provider.code if user.provider else None
    token = create_access_token(
        user.id, user.role,
        extra={"provider_id": user.provider_id, "customer_id": user.customer_id},
    )
    audit(db, user, "LOGIN", target=user.email)
    return TokenResponse(
        access_token=token,
        expires_in_minutes=settings.access_token_ttl_minutes,
        role=user.role, user_id=user.id, full_name=user.full_name,
        customer_id=user.customer_id, provider_id=user.provider_id,
        provider_code=provider_code,
    )


@auth_router.get("/me", response_model=UserOut)
def me(user: CurrentUser) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        is_active=user.is_active,
        customer_id=user.customer_id,
        provider_id=user.provider_id,
        provider_code=user.provider.code if user.provider else None,
    )


# ===========================================================================
# Customer portal / transactions
# ===========================================================================

txn_router = APIRouter(prefix="/api/v1/transactions", tags=["transactions"])


def _customer_for(user: User, db: Session) -> Customer:
    if user.role == UserRole.CUSTOMER.value and user.customer_id:
        customer = db.get(Customer, user.customer_id)
        if customer is not None:
            return customer
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": "no_customer_profile",
                "message": "this account is not linked to a customer profile"},
    )


@txn_router.post("", response_model=TransactionDetail,
                 status_code=status.HTTP_201_CREATED)
async def create(
    req: CreateTransactionRequest,
    user: CurrentUser,
    customer_id: str | None = Query(
        None, description="ADMIN only: create on behalf of a customer"
    ),
    db: Session = Depends(get_db),
) -> TransactionDetail:
    if user.role == UserRole.ADMIN.value and customer_id:
        customer = db.get(Customer, customer_id)
        if customer is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": "customer not found"},
            )
    else:
        customer = _customer_for(user, db)

    nlp = (
        {"requested_component_sku": req.requested_component_sku}
        if req.requested_component_sku else None
    )
    txn = create_transaction(
        db, customer=customer, order_ref=req.order_ref,
        serial_number=req.serial_number, issue_type=req.issue_type,
        issue_description=req.issue_description, urgency=req.urgency,
        nlp_extraction=nlp,
    )
    audit(db, user, "CREATE_TRANSACTION", target=txn.reference, transaction_id=txn.id)

    if not req.defer_start:
        # auto_repair is an operator/testing convenience; a customer cannot use
        # it to fake repair progress at the service centre.
        auto = req.auto_repair and user.role == UserRole.ADMIN.value
        await run_transaction(db, txn, auto_repair=auto)
    db.flush()
    return serializers.to_detail(txn, db)


@txn_router.post("/natural-language", response_model=TransactionDetail,
                 status_code=status.HTTP_201_CREATED)
async def create_from_text(
    req: NaturalLanguageRequest,
    user: CurrentUser,
    db: Session = Depends(get_db),
) -> TransactionDetail:
    """Create a transaction from a free-text problem description.

    Extraction produces a *suggestion*. Everything downstream - warranty,
    coverage, compatibility, authorization - is decided by the deterministic
    engines against the organizations' own data, never by the extractor.
    """
    from servicemesh.genai.extract import extract_issue

    customer = _customer_for(user, db)
    extraction = extract_issue(req.text)

    if extraction.requires_human_review and not req.serial_number:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "extraction_uncertain",
                "message": (
                    "could not confidently determine the issue type from the "
                    "description; please select it manually"
                ),
                "detail": extraction.to_dict(),
            },
        )

    order_ref = req.order_ref or extraction.order_ref
    serial = req.serial_number or extraction.serial_number
    if not order_ref or not serial:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "missing_identifiers",
                "message": "order reference and serial number are required",
                "detail": extraction.to_dict(),
            },
        )

    txn = create_transaction(
        db, customer=customer, order_ref=order_ref, serial_number=serial,
        issue_type=extraction.issue_type, issue_description=req.text,
        urgency=extraction.urgency, raw_request_text=req.text,
        nlp_extraction=extraction.to_dict(),
    )
    audit(db, user, "CREATE_TRANSACTION_NL", target=txn.reference, transaction_id=txn.id)
    await run_transaction(
        db, txn, auto_repair=req.auto_repair and user.role == UserRole.ADMIN.value
    )
    db.flush()
    return serializers.to_detail(txn, db)


@txn_router.get("", response_model=list[TransactionSummary])
def list_transactions(
    user: CurrentUser,
    state: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[TransactionSummary]:
    stmt = select(ServiceTransaction).order_by(ServiceTransaction.created_at.desc())

    if user.role == UserRole.CUSTOMER.value:
        stmt = stmt.where(ServiceTransaction.customer_id == user.customer_id)
    elif user.role == UserRole.PROVIDER.value:
        from servicemesh.core.models import TransactionParticipant

        stmt = stmt.where(
            ServiceTransaction.id.in_(
                select(TransactionParticipant.transaction_id).where(
                    TransactionParticipant.provider_id == user.provider_id
                )
            )
        )
    if state:
        stmt = stmt.where(ServiceTransaction.state == state.upper())

    rows = db.scalars(stmt.limit(limit).offset(offset))
    return [serializers.to_summary(t) for t in rows]


@txn_router.get("/{transaction_id}", response_model=TransactionDetail)
def get_transaction(
    transaction_id: str, user: CurrentUser, db: Session = Depends(get_db)
) -> TransactionDetail:
    txn = get_owned_transaction(transaction_id, user, db)
    return serializers.to_detail(txn, db)


@txn_router.get("/{transaction_id}/status")
def get_status(
    transaction_id: str, user: CurrentUser, db: Session = Depends(get_db)
) -> dict:
    txn = get_owned_transaction(transaction_id, user, db)
    return {
        "transaction_id": txn.id, "reference": txn.reference, "state": txn.state,
        "state_label": serializers.STATE_LABELS.get(txn.state, txn.state.replace("_", " ").title()),
        "next_action": serializers.STATE_NEXT_ACTIONS.get(txn.state, "Review request status"),
        "resume_state": txn.resume_state,
        "progress_percent": serializers.progress_percent(txn),
        "outcome": txn.outcome, "outcome_reason": txn.outcome_reason,
        "requires_manual_intervention": txn.requires_manual_intervention,
        "sla_breached": txn.sla_breached, "updated_at": txn.updated_at,
    }


@txn_router.get("/{transaction_id}/history")
def get_history(
    transaction_id: str, user: CurrentUser, db: Session = Depends(get_db)
) -> dict:
    txn = get_owned_transaction(transaction_id, user, db)
    detail = serializers.to_detail(txn, db)
    return {
        "transaction_id": txn.id,
        "timeline": [n.model_dump() for n in detail.timeline],
        "state_history": [h.model_dump() for h in detail.state_history],
        "operations": [o.model_dump() for o in detail.operations],
        "failures": [f.model_dump() for f in detail.failures],
        "recovery_actions": [r.model_dump() for r in detail.recovery_actions],
        "decisions": [d.model_dump() for d in detail.decisions],
        "events": [e.model_dump() for e in detail.events],
    }


@txn_router.post("/{transaction_id}/resume", response_model=DriveResponse)
async def resume(
    transaction_id: str,
    user: CurrentUser,
    auto_repair: bool = Query(False),
    db: Session = Depends(get_db),
) -> DriveResponse:
    """Operator-initiated resume. Resumes; never restarts."""
    txn = get_owned_transaction(transaction_id, user, db)
    if user.role not in {UserRole.ADMIN.value, UserRole.CUSTOMER.value}:
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden", "message": "providers cannot resume"},
        )
    if TransactionState(txn.state) in TERMINAL_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "terminal_state",
                    "message": f"transaction is {txn.state} and cannot be resumed"},
        )
    result = await resume_transaction(
        db, txn, actor=user.role,
        auto_repair=auto_repair and user.role == UserRole.ADMIN.value,
    )
    audit(db, user, "RESUME_TRANSACTION", target=txn.reference, transaction_id=txn.id)
    db.flush()
    return DriveResponse(
        transaction_id=txn.id, reference=txn.reference,
        final_state=result.final_state.value, steps_executed=result.steps_executed,
        recoveries_applied=result.recoveries_applied,
        halted_reason=result.halted_reason,
    )


@txn_router.post("/{transaction_id}/cancel", response_model=DriveResponse)
async def cancel(
    transaction_id: str, user: CurrentUser, db: Session = Depends(get_db)
) -> DriveResponse:
    """Cancel and compensate any committed side effects."""
    from servicemesh.orchestration.workflow import Workflow

    txn = get_owned_transaction(transaction_id, user, db)
    if TransactionState(txn.state) in TERMINAL_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "terminal_state", "message": f"already {txn.state}"},
        )

    wf = Workflow(db)
    wf.sm.transition(
        txn, TransactionState.COMPENSATING,
        reason=f"cancellation requested by {user.role}", actor=user.role,
    )
    undone = await wf.orc.compensate(txn, reason="transaction cancelled")
    txn.outcome = "CANCELLED"
    txn.outcome_reason = "cancelled by " + user.role.lower()
    wf.sm.transition(
        txn, TransactionState.CANCELLED,
        reason="; ".join(undone) if undone else "no side effects required undoing",
        actor=user.role, force=True,
    )
    audit(db, user, "CANCEL_TRANSACTION", target=txn.reference, transaction_id=txn.id)
    db.flush()
    return DriveResponse(
        transaction_id=txn.id, reference=txn.reference, final_state=txn.state,
        steps_executed=0, recoveries_applied=0,
        halted_reason="; ".join(undone) if undone else "cancelled",
    )


@txn_router.get("/{transaction_id}/customers/me", response_model=CustomerOut)
def my_customer(
    transaction_id: str, user: CurrentUser, db: Session = Depends(get_db)
) -> Customer:
    txn = get_owned_transaction(transaction_id, user, db)
    return txn.customer


@txn_router.get("/{transaction_id}/notifications")
def transaction_notifications(transaction_id: str, user: CurrentUser, db: Session = Depends(get_db)) -> list[dict]:
    from servicemesh.core.models import Notification
    txn = get_owned_transaction(transaction_id, user, db)
    rows = db.scalars(select(Notification).where(
        Notification.transaction_id == txn.id, Notification.recipient_id == txn.customer_id
    ).order_by(Notification.created_at.desc()))
    return [{"id": n.id, "title": n.title, "body": n.body, "is_read": n.is_read, "created_at": n.created_at} for n in rows]


@txn_router.post("/{transaction_id}/notifications/{notification_id}/read")
def mark_notification_read(
    transaction_id: str, notification_id: str, user: CurrentUser,
    db: Session = Depends(get_db),
) -> dict:
    from servicemesh.core.models import Notification
    txn = get_owned_transaction(transaction_id, user, db)
    notification = db.scalar(select(Notification).where(
        Notification.id == notification_id,
        Notification.transaction_id == txn.id,
        Notification.recipient_id == user.customer_id,
    ))
    if notification is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "notification not found"})
    notification.is_read = True
    return {"id": notification.id, "is_read": True}



# ===========================================================================
# Provider portal
# ===========================================================================

provider_router = APIRouter(prefix="/api/v1/providers", tags=["providers"])


@provider_router.get("/me", response_model=ProviderStats)
def my_provider(user: CurrentUser, db: Session = Depends(get_db)) -> ProviderStats:
    if user.provider_id is None:
        raise HTTPException(
            status_code=400,
            detail={"error": "no_provider", "message": "account is not a provider"},
        )
    p = db.get(Provider, user.provider_id)
    return _provider_stats(p)


@provider_router.get("/me/jobs", response_model=list[ProviderJobOut])
def my_jobs(
    user: User = Depends(require_repair_workflow),
    db: Session = Depends(get_db),
) -> list[ProviderJobOut]:
    """Operations belonging to the caller's organization only.

    A service centre must not be able to read the supplier's operations, so
    the filter is on provider_id from the token, never from a query parameter.
    """
    if user.provider_id is None:
        raise HTTPException(
            status_code=400,
            detail={"error": "no_provider", "message": "account is not a provider"},
        )

    provider = db.get(Provider, user.provider_id)
    if provider is None or provider.kind != "SERVICE_CENTRE":
        return []

    ops = db.scalars(
        select(ProviderOperation)
        .where(ProviderOperation.provider_id == user.provider_id)
        .order_by(ProviderOperation.created_at.desc())
        .limit(200)
    )
    jobs: list[ProviderJobOut] = []
    for op in ops:
        txn = op.transaction
        if txn is None:
            continue
        if (
            user.role == UserRole.REPAIR_PERSON.value
            and txn.selected_service_provider_id != user.provider_id
        ):
            continue
        from servicemesh.core.models import Component

        component = (
            db.get(Component, txn.selected_component_id)
            if txn.selected_component_id else None
        )
        jobs.append(ProviderJobOut(
            transaction_id=txn.id, reference=txn.reference,
            transaction_state=txn.state, operation_id=op.id,
            resume_state=txn.resume_state,
            operation_type=op.operation_type, operation_status=op.status,
            serial_number=txn.serial_number, issue_type=txn.issue_type,
            issue_description=txn.issue_description,
            customer_name=txn.customer.full_name if txn.customer else "",
            booking_ref=txn.service_booking_ref,
            reservation_ref=txn.part_reservation_ref,
            component_sku=component.sku if component else None,
            assigned_repair_person=txn.assigned_repair_person,
            repair_phase=(
                "COMPLETED" if txn.state in {
                    TransactionState.REPAIR_COMPLETED.value,
                    TransactionState.SERVICE_VERIFIED.value,
                    TransactionState.CLOSED.value,
                }
                else "REPAIRING" if txn.repair_notes
                else "DIAGNOSING" if txn.diagnosis_notes
                else None
            ),
            part_delivery_status=txn.part_delivery_status,
            part_eta=txn.part_eta,
            created_at=op.created_at,
        ))
    return jobs


@provider_router.get("/me/supplier-jobs", response_model=list[ProviderJobOut])
def supplier_jobs(
    user: User = Depends(require_provider_or_admin),
    db: Session = Depends(get_db),
) -> list[ProviderJobOut]:
    """Supplier-facing reservation and dispatch queue, scoped to its provider."""
    if user.provider_id is None:
        raise HTTPException(status_code=400, detail={"error": "no_provider", "message": "account is not a provider"})
    provider = db.get(Provider, user.provider_id)
    if provider is None or provider.kind != "PARTS_SUPPLIER":
        return []
    ops = db.scalars(select(ProviderOperation).where(
        ProviderOperation.provider_id == provider.id,
        ProviderOperation.operation_type.in_({
            OperationType.RESERVE_PART.value, OperationType.DISPATCH_PART.value,
        }),
    ).order_by(ProviderOperation.created_at.desc()).limit(200))
    result: list[ProviderJobOut] = []
    for op in ops:
        txn = op.transaction
        if txn is None:
            continue
        component = db.get(Component, txn.selected_component_id) if txn.selected_component_id else None
        result.append(ProviderJobOut(
            transaction_id=txn.id, reference=txn.reference,
            transaction_state=txn.state, resume_state=txn.resume_state,
            operation_id=op.id, operation_type=op.operation_type,
            operation_status=op.status, serial_number=txn.serial_number,
            issue_type=txn.issue_type, issue_description=txn.issue_description,
            customer_name=txn.customer.full_name if txn.customer else "",
            reservation_ref=txn.part_reservation_ref,
            component_sku=component.sku if component else None,
            part_delivery_status=txn.part_delivery_status, part_eta=txn.part_eta,
            created_at=op.created_at,
        ))
    return result


@provider_router.get("/me/workload")
def my_workload(
    user: User = Depends(require_repair_workflow),
    db: Session = Depends(get_db),
) -> dict:
    if user.provider_id is None:
        raise HTTPException(status_code=400, detail={"error": "no_provider", "message": "account is not a provider"})
    provider = db.get(Provider, user.provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "provider not found"})
    active_states = {s.value for s in TERMINAL_STATES}
    active = db.scalar(select(func.count()).select_from(ProviderOperation).join(ServiceTransaction)
        .where(ProviderOperation.provider_id == provider.id,
               ServiceTransaction.state.not_in(active_states))) or 0
    return {
        "provider_id": provider.id, "provider_code": provider.code,
        "capacity_total": provider.capacity_total, "capacity_used": provider.capacity_used,
        "available_capacity": provider.available_capacity,
        "active_jobs": int(active), "utilization_percent": round(
            provider.capacity_used / provider.capacity_total * 100, 1
        ) if provider.capacity_total else 0.0,
    }


@provider_router.get("/me/repair-persons", response_model=list[UserOut])
def assignable_repair_persons(
    user: User = Depends(require_provider_or_admin),
    db: Session = Depends(get_db),
) -> list[UserOut]:
    """Return active technicians that can be selected for service work.

    Provider users are restricted to their own service centre.  Admins have no
    provider scope, so they receive the complete active technician directory.
    """
    stmt = select(User).where(
        User.role == UserRole.REPAIR_PERSON.value,
        User.is_active.is_(True),
    ).order_by(User.full_name)
    if user.provider_id is not None:
        stmt = stmt.where(User.provider_id == user.provider_id)
    return [
        UserOut(
            id=technician.id,
            email=technician.email,
            full_name=technician.full_name,
            role=technician.role,
            is_active=technician.is_active,
            customer_id=technician.customer_id,
            provider_id=technician.provider_id,
            provider_code=technician.provider.code if technician.provider else None,
        )
        for technician in db.scalars(stmt)
    ]


@provider_router.post("/me/transactions/{transaction_id}/repair",
                      response_model=DriveResponse)
async def update_repair(
    transaction_id: str,
    req: RepairUpdateRequest,
    user: User = Depends(require_repair_workflow),
    db: Session = Depends(get_db),
) -> DriveResponse:
    """A service centre reports repair progress; ServiceMesh continues."""
    txn = get_owned_transaction(transaction_id, user, db)
    if (user.role in {
            UserRole.PROVIDER.value, UserRole.REPAIR_PERSON.value
        }
            and txn.selected_service_provider_id != user.provider_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "forbidden",
                    "message": "this transaction is not assigned to your organization"},
        )
    if txn.service_booking_ref is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "no_booking",
                    "message": "transaction has no service booking yet"},
        )

    try:
        result = await advance_repair(
            db, txn, status=req.status, technician=req.technician,
            notes=req.notes, actor=user.role
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "invalid_repair_progression", "message": str(exc)},
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "provider_update_failed", "message": str(exc)},
        ) from exc

    audit(db, user, "UPDATE_REPAIR", target=txn.reference, transaction_id=txn.id,
          details={"status": req.status})
    db.flush()
    return DriveResponse(
        transaction_id=txn.id, reference=txn.reference,
        final_state=result.final_state.value, steps_executed=result.steps_executed,
        recoveries_applied=result.recoveries_applied,
        halted_reason=result.halted_reason,
        message=(
            f"repair status {req.status} recorded; transaction is "
            f"{result.final_state.value}"
        ),
        repair_status=req.status,
        next_actions=(
            ["REPAIRING", "COMPLETED"] if req.status in {"DIAGNOSING", "AWAITING_PART"}
            else ["COMPLETED"] if req.status == "REPAIRING"
            else []
        ),
    )


@provider_router.post("/me/transactions/{transaction_id}/service-decision", response_model=DriveResponse)
async def service_decision(
    transaction_id: str, action: str = Query(..., pattern="^(ACCEPT|REJECT)$"),
    user: User = Depends(require_provider_or_admin), db: Session = Depends(get_db),
) -> DriveResponse:
    """Service-centre acceptance or rejection; rejection triggers provider fallback."""
    if user.provider_id is None:
        raise HTTPException(status_code=400, detail={"error": "no_provider", "message": "account is not a provider"})
    txn = get_owned_transaction(transaction_id, user, db)
    if user.role == UserRole.PROVIDER.value and txn.selected_service_provider_id != user.provider_id:
        raise HTTPException(status_code=403, detail={"error": "forbidden", "message": "transaction is not assigned to your service centre"})
    from servicemesh.orchestration.workflow import Workflow
    wf = Workflow(db)
    provider = db.get(Provider, user.provider_id)
    if action == "ACCEPT":
        _, result = await wf.orc.execute_operation(txn, OperationType.UPDATE_REPAIR, provider,
            {"booking_ref": txn.service_booking_ref, "status": "ACCEPTED", "notes": "service centre accepted job"})
        if not result.success:
            raise HTTPException(status_code=502, detail={"error": "accept_failed", "message": result.error_message or "accept failed"})
        audit(db, user, "SERVICE_JOB_ACCEPTED", target=txn.reference, transaction_id=txn.id)
        db.flush()
        return DriveResponse(transaction_id=txn.id, reference=txn.reference, final_state=txn.state, steps_executed=0, recoveries_applied=0, halted_reason="job accepted")

    # REJECT: cancel the existing booking, exclude this provider, and resume
    # from provider selection. Completed purchase/product/warranty work is not repeated.
    if txn.service_booking_ref:
        _, cancel_result = await wf.orc.execute_operation(
            txn, OperationType.CANCEL_SERVICE, provider,
            {"booking_ref": txn.service_booking_ref},
        )
        if not cancel_result.success:
            raise HTTPException(status_code=502, detail={"error": "cancel_failed", "message": cancel_result.error_message or "booking cancellation failed"})
    txn.excluded_provider_ids = list(set((txn.excluded_provider_ids or []) + [provider.id]))
    txn.selected_service_provider_id = None
    txn.service_booking_ref = None
    wf.sm.transition(txn, TransactionState.COVERAGE_CHECKED, reason=f"{provider.code} rejected service job; selecting fallback", actor=user.role, force=True)
    audit(db, user, "SERVICE_JOB_REJECTED", target=txn.reference, transaction_id=txn.id, details={"provider": provider.code})
    result = await wf.drive(txn)
    db.flush()
    return DriveResponse(transaction_id=txn.id, reference=txn.reference, final_state=result.final_state.value,
                         steps_executed=result.steps_executed, recoveries_applied=result.recoveries_applied, halted_reason=result.halted_reason)


@provider_router.post("/me/transactions/{transaction_id}/supplier-dispatch",
                      response_model=DriveResponse)
async def supplier_dispatch(
    transaction_id: str,
    req: SupplierDispatchRequest,
    user: User = Depends(require_provider_or_admin),
    db: Session = Depends(get_db),
) -> DriveResponse:
    """Supplier portal action against the supplier's real simulator API."""
    if user.provider_id is None:
        raise HTTPException(status_code=400, detail={"error": "no_provider", "message": "account is not a provider"})
    txn = get_owned_transaction(transaction_id, user, db)
    if user.role == UserRole.PROVIDER.value and txn.selected_supplier_id != user.provider_id:
        raise HTTPException(status_code=403, detail={"error": "forbidden", "message": "transaction is not assigned to your supplier"})
    if not txn.part_reservation_ref:
        raise HTTPException(status_code=409, detail={"error": "no_reservation", "message": "no part reservation exists"})
    from servicemesh.orchestration.workflow import Workflow
    provider = db.get(Provider, user.provider_id)
    wf = Workflow(db)
    op, result = await wf.orc.execute_operation(
        txn, OperationType.DISPATCH_PART, provider,
        {"reservation_ref": txn.part_reservation_ref, "eta": req.eta},
    )
    if not result.success:
        raise HTTPException(status_code=502, detail={"error": "dispatch_failed", "message": result.error_message or "supplier dispatch failed"})
    txn.part_delivery_status = result.data.get("status", "DISPATCHED")
    txn.part_eta = result.data.get("eta") or req.eta or txn.part_eta
    txn.supplier_decision = "ACCEPTED"
    audit(db, user, "DISPATCH_PART", target=txn.reference, transaction_id=txn.id)
    db.flush()
    return DriveResponse(transaction_id=txn.id, reference=txn.reference, final_state=txn.state,
                         steps_executed=0, recoveries_applied=0, halted_reason="part dispatched by supplier")


@provider_router.get("", response_model=list[ProviderStats])
def list_providers(
    user: User = Depends(require_admin),
    kind: str | None = Query(None),
    db: Session = Depends(get_db),
) -> list[ProviderStats]:
    stmt = select(Provider)
    if kind:
        stmt = stmt.where(Provider.kind == kind.upper())
    return [_provider_stats(p) for p in db.scalars(stmt.order_by(Provider.kind))]


def _provider_stats(p: Provider) -> ProviderStats:
    return ProviderStats(
        id=p.id, code=p.code, name=p.name, kind=p.kind, region=p.region,
        is_active=p.is_active, sla_hours=p.sla_hours, cost_index=p.cost_index,
        capacity_total=p.capacity_total, capacity_used=p.capacity_used,
        total_operations=p.total_operations,
        successful_operations=p.successful_operations,
        failed_operations=p.failed_operations, sla_violations=p.sla_violations,
        success_rate=round(p.success_rate, 4),
        avg_latency_ms=round(p.avg_latency_ms, 1),
        available_capacity=p.available_capacity,
    )


# ===========================================================================
# Admin / operations
# ===========================================================================

admin_router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@admin_router.get("/metrics", response_model=DashboardMetrics)
def metrics(
    user: User = Depends(require_admin), db: Session = Depends(get_db)
) -> DashboardMetrics:
    """Every figure is computed from persisted rows. Nothing is hard-coded."""
    total = db.scalar(select(func.count()).select_from(ServiceTransaction)) or 0

    by_state: dict[str, int] = {}
    for state, count in db.execute(
        select(ServiceTransaction.state, func.count()).group_by(ServiceTransaction.state)
    ):
        by_state[state] = count

    completed = by_state.get(TransactionState.CLOSED.value, 0)
    failed = by_state.get(TransactionState.FAILED.value, 0)
    rejected = by_state.get(TransactionState.REJECTED.value, 0)
    escalated = by_state.get(TransactionState.ESCALATED.value, 0)
    recovering = (
        by_state.get(TransactionState.RETRYING.value, 0)
        + by_state.get(TransactionState.COMPENSATING.value, 0)
    )
    terminal = {s.value for s in TERMINAL_STATES}
    in_progress = sum(v for k, v in by_state.items() if k not in terminal)

    sla_breaches = db.scalar(
        select(func.count()).select_from(ServiceTransaction)
        .where(ServiceTransaction.sla_breached.is_(True))
    ) or 0
    manual = db.scalar(
        select(func.coalesce(func.sum(ServiceTransaction.manual_interventions), 0))
    ) or 0
    retries = db.scalar(
        select(func.coalesce(func.sum(ServiceTransaction.retry_total), 0))
    ) or 0
    total_failures = db.scalar(select(func.count()).select_from(FailureRecord)) or 0
    total_recoveries = db.scalar(select(func.count()).select_from(RecoveryAction)) or 0
    recoveries_ok = db.scalar(
        select(func.count()).select_from(RecoveryAction)
        .where(RecoveryAction.succeeded.is_(True))
    ) or 0

    # An operation whose attempt count exceeds one but which produced a single
    # business side effect is a duplicate the idempotency layer prevented.
    duplicates_prevented = db.scalar(
        select(func.count()).select_from(ProviderOperation).where(
            ProviderOperation.attempt_count > 1,
            ProviderOperation.idempotency_key.is_not(None),
            ProviderOperation.status == OperationStatus.SUCCEEDED.value,
        )
    ) or 0

    total_ops = db.scalar(select(func.count()).select_from(ProviderOperation)) or 0

    closed = list(db.scalars(
        select(ServiceTransaction).where(ServiceTransaction.closed_at.is_not(None))
    ))
    durations = []
    for t in closed:
        start, end = t.created_at, t.closed_at
        if start and end:
            if start.tzinfo is None:
                start = start.replace(tzinfo=UTC)
            if end.tzinfo is None:
                end = end.replace(tzinfo=UTC)
            durations.append((end - start).total_seconds())

    return DashboardMetrics(
        total_transactions=total, by_state=by_state, completed=completed,
        failed=failed, rejected=rejected, escalated=escalated,
        in_progress=in_progress, recovering=recovering, sla_breaches=sla_breaches,
        manual_interventions=int(manual), total_retries=int(retries),
        total_failures=total_failures, total_recovery_actions=total_recoveries,
        recovery_success_rate=(
            round(recoveries_ok / total_recoveries, 4) if total_recoveries else 0.0
        ),
        duplicate_operations_prevented=duplicates_prevented,
        avg_resolution_seconds=(
            round(sum(durations) / len(durations), 2) if durations else None
        ),
        avg_operations_per_transaction=(
            round(total_ops / total, 2) if total else 0.0
        ),
        completion_rate=round(completed / total, 4) if total else 0.0,
        generated_at=datetime.now(UTC),
    )


@admin_router.get("/customers", response_model=list[CustomerOut])
def list_customers(
    user: User = Depends(require_admin), db: Session = Depends(get_db)
) -> list[Customer]:
    return list(db.scalars(select(Customer).order_by(Customer.external_ref)))


@admin_router.post("/transactions/{transaction_id}/intervene", response_model=DriveResponse)
async def intervene(
    transaction_id: str,
    req: AdminInterventionRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> DriveResponse:
    """Allow an administrator to explicitly unblock or escalate a workflow."""
    txn = db.get(ServiceTransaction, transaction_id)
    if txn is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "transaction not found"})
    if req.action == "ESCALATE":
        from servicemesh.orchestration.workflow import Workflow
        wf = Workflow(db)
        wf.sm.transition(txn, TransactionState.ESCALATED, reason=req.reason, actor="ADMIN", force=True)
        audit(db, user, "ADMIN_ESCALATE", target=txn.reference, transaction_id=txn.id,
              details={"reason": req.reason})
        db.flush()
        return DriveResponse(
            transaction_id=txn.id, reference=txn.reference, final_state=txn.state,
            steps_executed=0, recoveries_applied=0, halted_reason=req.reason,
            next_actions=["ADMIN_RESUME"],
        )
    if TransactionState(txn.state) in TERMINAL_STATES:
        raise HTTPException(status_code=409, detail={"error": "terminal_state", "message": f"transaction is {txn.state}"})
    result = await resume_transaction(db, txn, actor="ADMIN")
    txn.requires_manual_intervention = False
    audit(db, user, "ADMIN_RESUME", target=txn.reference, transaction_id=txn.id,
          details={"reason": req.reason})
    db.flush()
    return DriveResponse(
        transaction_id=txn.id, reference=txn.reference,
        final_state=result.final_state.value, steps_executed=result.steps_executed,
        recoveries_applied=result.recoveries_applied, halted_reason=result.halted_reason,
    )


@admin_router.get("/recovery-matrix")
def recovery_matrix(user: User = Depends(require_admin)) -> dict:
    """The failure/recovery matrix as implemented, read from the engine."""
    return {"version": "recovery-v1", "rules": describe_matrix()}


@admin_router.get("/policies")
def policies(user: User = Depends(require_admin)) -> dict:
    from servicemesh.engines.policy import PolicyEngine

    return PolicyEngine().describe()


@admin_router.get("/simulation")
def simulation_status(user: User = Depends(require_admin)) -> dict:
    return simulation.status() | {"available_modes": simulation.modes()}


@admin_router.post("/simulation")
def set_simulation(
    req: SimulationRequest, user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Push an organization into a failure mode for a demonstration.

    Routed through the simulation controller, which talks to the organization
    over HTTP when it runs as a separate service. A request that cannot reach
    the organization fails loudly instead of quietly doing nothing.
    """
    try:
        result = simulation.set_mode(
            req.service, req.mode, req.operation, req.count, req.latency_seconds
        )
    except simulation.SimulationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": "simulation_failed", "message": exc.message},
        ) from exc
    audit(db, user, "SET_SIMULATION", target=req.service,
          details={"mode": req.mode, "operation": req.operation,
                   "channel": result.get("channel")})
    return result


@admin_router.post("/simulation/reset")
def reset_simulation(
    user: User = Depends(require_admin), db: Session = Depends(get_db),
    service: str | None = Query(None),
) -> dict:
    try:
        result = simulation.reset(service)
    except simulation.SimulationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": "simulation_failed", "message": exc.message},
        ) from exc
    audit(db, user, "RESET_SIMULATION", target=service or "all")
    return result
