"""Service-layer entry points for Service Transactions.

Kept separate from the API so the same operations are callable from the REST
layer, the experiment harness, the demo scripts and the tests without any of
them depending on HTTP.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from servicemesh.core.config import get_settings
from servicemesh.core.enums import TransactionState
from servicemesh.core.models import (
    Customer,
    ServiceTransaction,
    TransactionStateHistory,
    new_uuid,
    utcnow,
)
from servicemesh.orchestration.orchestrator import DriveResult
from servicemesh.orchestration.workflow import Workflow

logger = logging.getLogger("servicemesh.service")


def next_reference(db: Session) -> str:
    """Human-facing reference, e.g. SM-2026-000123."""
    year = utcnow().year
    count = db.scalar(
        select(func.count()).select_from(ServiceTransaction)
    ) or 0
    return f"SM-{year}-{count + 1:06d}"


def create_transaction(
    db: Session,
    *,
    customer: Customer,
    order_ref: str,
    serial_number: str,
    issue_type: str,
    issue_description: str,
    urgency: str = "NORMAL",
    raw_request_text: str | None = None,
    nlp_extraction: dict | None = None,
    sla_hours: int | None = None,
) -> ServiceTransaction:
    """Create a Service Transaction in CREATED and record its opening history."""
    settings = get_settings()
    sla_hours = sla_hours or settings.default_sla_hours

    txn = ServiceTransaction(
        reference=next_reference(db),
        correlation_id=new_uuid(),
        customer_id=customer.id,
        state=TransactionState.CREATED.value,
        resume_state=TransactionState.CREATED.value,
        order_ref=order_ref,
        serial_number=serial_number,
        issue_type=issue_type.upper(),
        issue_description=issue_description,
        urgency=urgency.upper(),
        raw_request_text=raw_request_text,
        nlp_extraction=nlp_extraction,
        sla_due_at=utcnow() + timedelta(hours=sla_hours),
        excluded_provider_ids=[],
        excluded_component_ids=[],
    )
    db.add(txn)
    db.flush()

    db.add(TransactionStateHistory(
        transaction_id=txn.id, from_state=None,
        to_state=TransactionState.CREATED.value,
        reason="customer submitted an after-sales service request",
        actor="CUSTOMER",
        context={"issue_type": txn.issue_type, "order_ref": order_ref,
                 "serial_number": serial_number},
    ))

    from servicemesh.core.enums import EventType
    from servicemesh.events.bus import DomainEvent, event_bus

    event_bus.publish(db, DomainEvent(
        event_type=EventType.TRANSACTION_CREATED,
        transaction_id=txn.id, correlation_id=txn.correlation_id,
        payload={"reference": txn.reference, "issue_type": txn.issue_type,
                 "serial_number": serial_number},
    ))
    db.flush()
    logger.info("created transaction %s for customer %s", txn.reference, customer.external_ref)
    return txn


async def run_transaction(
    db: Session, txn: ServiceTransaction, *, auto_repair: bool = False, **kwargs
) -> DriveResult:
    """Drive a transaction as far as it can currently go."""
    workflow = Workflow(db, **kwargs)
    return await workflow.drive(txn, auto_repair=auto_repair)


async def resume_transaction(
    db: Session, txn: ServiceTransaction, *, actor: str = "OPERATOR", auto_repair: bool = False
) -> DriveResult:
    """Operator-initiated resume of an escalated or waiting transaction.

    Moves the transaction back to its last safe milestone and drives again.
    Everything already completed stays completed.
    """
    workflow = Workflow(db)
    current = TransactionState(txn.state)
    if current in {TransactionState.ESCALATED, TransactionState.WAITING,
                   TransactionState.FAILED, TransactionState.TIMEOUT,
                   TransactionState.RETRYING}:
        cleared = _clear_transient_exclusions(db, txn)
        resume = TransactionState(txn.resume_state)
        txn.requires_manual_intervention = False
        if cleared:
            logger.info(
                "txn=%s resume cleared %d transient provider exclusion(s)",
                txn.reference, len(cleared),
            )
        workflow.sm.transition(
            txn, resume,
            reason=f"{actor} resumed the transaction from {resume.value}",
            actor=actor, force=True, emit_event=False,
        )
        db.flush()
    return await workflow.drive(txn, auto_repair=auto_repair)


def _clear_transient_exclusions(db: Session, txn: ServiceTransaction) -> list[str]:
    """Re-admit providers that were excluded only because of a transient fault.

    When a supplier is skipped because it was timing out, that exclusion is a
    statement about a moment, not about the provider. If an operator resumes
    the transaction after the outage clears, keeping the exclusion would leave
    nothing eligible and the transaction would escalate again immediately.

    Exclusions from *permanent* causes are kept: an OEM that does not authorize
    a partner will not authorize it ten minutes later either.
    """
    from servicemesh.core.models import RecoveryAction

    actions = db.scalars(
        select(RecoveryAction).where(RecoveryAction.transaction_id == txn.id)
    )
    transient_ids = {
        a.inputs.get("provider_id")
        for a in actions
        if a.inputs.get("failure_type") == "TRANSIENT" and a.inputs.get("provider_id")
    }
    if not transient_ids:
        return []

    excluded = list(txn.excluded_provider_ids or [])
    remaining = [pid for pid in excluded if pid not in transient_ids]
    cleared = [pid for pid in excluded if pid in transient_ids]
    txn.excluded_provider_ids = remaining
    db.flush()
    return cleared


async def advance_repair(
    db: Session,
    txn: ServiceTransaction,
    *,
    status: str,
    technician: str | None = None,
    notes: str | None = None,
    actor: str = "PROVIDER",
) -> DriveResult:
    """Called by the Provider Portal when a service centre reports progress.

    The service centre is the authority on repair status; ServiceMesh reflects
    what it reports and then continues the workflow.
    """
    from servicemesh.core.enums import OperationType
    from servicemesh.core.models import Provider

    workflow = Workflow(db)
    provider = db.get(Provider, txn.selected_service_provider_id)
    if provider is None:
        raise ValueError("transaction has no selected service provider")

    current_state = TransactionState(txn.state)
    # A completion report is not allowed to skip the technician work.  The
    # provider simulator accepts any status, so this business rule belongs in
    # the core service rather than in the adapter or the HTTP layer.
    if status == "COMPLETED" and current_state is not TransactionState.REPAIR_IN_PROGRESS:
        raise ValueError(
            f"cannot complete repair while transaction is {current_state.value}; "
            "report DIAGNOSING/REPAIRING first"
        )
    if status == "REPAIRING" and current_state not in {
        TransactionState.REPAIR_SCHEDULED,
        TransactionState.REPAIR_IN_PROGRESS,
        TransactionState.WAITING,
    }:
        raise ValueError(
            f"cannot start repair while transaction is {current_state.value}"
        )
    if status == "COMPLETED" and not txn.repair_notes:
        raise ValueError("cannot complete repair before repair has been started")
    if status == "VERIFIED" and current_state is not TransactionState.REPAIR_COMPLETED:
        raise ValueError(
            f"cannot verify repair while transaction is {current_state.value}; "
            "report COMPLETED first"
        )

    _, result = await workflow.orc.execute_operation(
        txn, OperationType.UPDATE_REPAIR, provider,
        {
            "booking_ref": txn.service_booking_ref,
            "status": status,
            "technician": technician,
            "notes": notes,
        },
    )
    if not result.success:
        raise RuntimeError(result.error_message or "service centre update failed")

    # The service centre is authoritative for the assigned repair person. Keep
    # the value returned by it (including on a replay), while accepting an
    # explicit portal assignment for providers that use named technicians.
    returned_technician = result.data.get("technician")
    if returned_technician:
        txn.assigned_repair_person = returned_technician
    elif technician:
        txn.assigned_repair_person = technician

    # Keep direct provider integrations backward-compatible: an older client
    # may report REPAIRING with its diagnosis in the same request. The portal
    # still presents Diagnose and Start repair as separate ordered actions.
    if status == "REPAIRING" and not txn.diagnosis_notes:
        txn.diagnosis_notes = notes or "Diagnosis recorded before repair start."

    mapping = {
        "REPAIRING": TransactionState.REPAIR_IN_PROGRESS,
        "DIAGNOSING": TransactionState.REPAIR_IN_PROGRESS,
        "AWAITING_PART": TransactionState.REPAIR_IN_PROGRESS,
        "COMPLETED": TransactionState.REPAIR_COMPLETED,
    }
    target = mapping.get(status)
    if status in {"DIAGNOSING", "AWAITING_PART"}:
        txn.diagnosis_notes = notes or result.data.get("notes") or txn.diagnosis_notes
    elif status in {"REPAIRING", "COMPLETED"}:
        txn.repair_notes = notes or result.data.get("notes") or txn.repair_notes
    elif status == "VERIFIED":
        txn.verification_result = "VERIFIED"
    # Repeated reports are idempotent from the transaction's point of view.
    # Do not create a misleading state change when the status is already
    # represented by the current state.
    if target is not None and target is not current_state:
        workflow.sm.transition(
            txn, target,
            reason=f"service centre reported status {status}"
            + (f": {notes}" if notes else ""),
            actor=actor,
            # REPAIR_SCHEDULED is intentionally the hand-off boundary: a
            # service centre may begin work without an orchestration call.
            force=current_state is TransactionState.WAITING,
        )
    db.flush()
    # Do not immediately run the normal waiting step after a human report:
    # that step deliberately parks the transaction in WAITING and would make
    # DIAGNOSING/REPAIRING look as if nothing happened.  Completion is
    # different: it must continue through confirmation and closure.
    if txn.state == TransactionState.REPAIR_IN_PROGRESS.value:
        return DriveResult(
            transaction_id=txn.id,
            final_state=TransactionState.REPAIR_IN_PROGRESS,
            steps_executed=0,
            recoveries_applied=0,
            halted_reason="repair progress recorded; awaiting next service-centre update",
        )
    return await workflow.drive(txn)
