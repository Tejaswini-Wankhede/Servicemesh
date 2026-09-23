"""Serialization from ORM state to API payloads.

The timeline deserves a note. The frontend must render something like

    Marketplace OK -> OEM OK -> Warranty OK -> Provider OK -> Compatibility OK
    -> Supplier WARN -> Timeout -> Retry -> Supplier OK -> Repair OK -> Closed

and every marker in that sequence has to come from the database. Nothing here
invents a status: a node is COMPLETED because a state-history row exists for
it, FAILED because a failure was recorded while the transaction was in it, and
CURRENT because it is the transaction's present state.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from servicemesh.api.schemas import (
    AttemptOut,
    DecisionOut,
    EventOut,
    FailureOut,
    OperationOut,
    ParticipantOut,
    RecoveryOut,
    StateHistoryOut,
    TimelineNode,
    TransactionDetail,
    TransactionSummary,
)
from servicemesh.core.enums import HAPPY_PATH, MUTATING_OPERATIONS, TransactionState
from servicemesh.core.models import (
    AuditEvent,
    Component,
    FailureRecord,
    Provider,
    ServiceTransaction,
    Notification,
)
from servicemesh.orchestration.state_machine import happy_index

STATE_LABELS: dict[str, str] = {
    "CREATED": "Request received",
    "PURCHASE_VERIFIED": "Purchase verified",
    "PRODUCT_VERIFIED": "Product verified",
    "WARRANTY_VERIFIED": "Warranty verified",
    "COVERAGE_CHECKED": "Coverage approved",
    "PROVIDER_SELECTED": "Service centre selected",
    "COMPONENT_VALIDATED": "Component validated",
    "PART_REQUESTED": "Part located",
    "PART_CONFIRMED": "Part reserved",
    "REPAIR_SCHEDULED": "Repair scheduled",
    "REPAIR_IN_PROGRESS": "Repair in progress",
    "REPAIR_COMPLETED": "Repair completed",
    "SERVICE_VERIFIED": "Service verified",
    "CLOSED": "Closed",
}

STATE_PROVIDER_KIND: dict[str, str] = {
    "PURCHASE_VERIFIED": "MARKETPLACE",
    "PRODUCT_VERIFIED": "MANUFACTURER",
    "WARRANTY_VERIFIED": "WARRANTY",
    "COVERAGE_CHECKED": "WARRANTY",
    "PROVIDER_SELECTED": "SERVICE_CENTRE",
    "PART_REQUESTED": "PARTS_SUPPLIER",
    "PART_CONFIRMED": "PARTS_SUPPLIER",
    "REPAIR_SCHEDULED": "SERVICE_CENTRE",
    "REPAIR_IN_PROGRESS": "SERVICE_CENTRE",
    "REPAIR_COMPLETED": "SERVICE_CENTRE",
    "SERVICE_VERIFIED": "SERVICE_CENTRE",
}

STATE_NEXT_ACTIONS: dict[str, str] = {
    "CREATED": "Verify purchase and product details",
    "PURCHASE_VERIFIED": "Verify the product",
    "PRODUCT_VERIFIED": "Confirm warranty coverage",
    "WARRANTY_VERIFIED": "Check service coverage",
    "COVERAGE_CHECKED": "Select an authorised service centre",
    "PROVIDER_SELECTED": "Validate the replacement component",
    "COMPONENT_VALIDATED": "Request the replacement part",
    "PART_REQUESTED": "Confirm the part reservation",
    "PART_CONFIRMED": "Schedule the repair",
    "REPAIR_SCHEDULED": "Check in the device for repair",
    "REPAIR_IN_PROGRESS": "Complete and verify the repair",
    "REPAIR_COMPLETED": "Verify service completion",
    "SERVICE_VERIFIED": "Close the request",
    "CLOSED": "No action needed",
    "ESCALATED": "Admin review required",
    "WAITING": "Wait for the next provider update",
    "RETRYING": "Retrying the provider operation",
    "FAILED": "Review the failed request",
    "REJECTED": "Review the rejected request",
    "CANCELLED": "No action needed",
}


def progress_percent(txn: ServiceTransaction) -> float:
    idx = happy_index(TransactionState(txn.resume_state))
    if idx is None:
        return 0.0
    return round(100.0 * idx / (len(HAPPY_PATH) - 1), 1)


def to_summary(txn: ServiceTransaction) -> TransactionSummary:
    return TransactionSummary(
        id=txn.id, reference=txn.reference, correlation_id=txn.correlation_id,
        state=txn.state, resume_state=txn.resume_state,
        state_label=STATE_LABELS.get(txn.state, txn.state.replace("_", " ").title()),
        next_action=STATE_NEXT_ACTIONS.get(txn.state, "Review request status"),
        progress_percent=progress_percent(txn),
        customer_id=txn.customer_id,
        customer_name=txn.customer.full_name if txn.customer else None,
        order_ref=txn.order_ref, serial_number=txn.serial_number,
        issue_type=txn.issue_type, urgency=txn.urgency,
        outcome=txn.outcome, outcome_reason=txn.outcome_reason,
        sla_due_at=txn.sla_due_at, sla_breached=txn.sla_breached,
        requires_manual_intervention=txn.requires_manual_intervention,
        retry_total=txn.retry_total,
        created_at=txn.created_at, updated_at=txn.updated_at, closed_at=txn.closed_at,
    )


def build_timeline(txn: ServiceTransaction, db: Session) -> list[TimelineNode]:
    """Derive the workflow visualisation entirely from persisted rows."""
    reached: dict[str, StateHistoryOut] = {}
    for h in txn.state_history:
        if h.to_state not in reached:
            reached[h.to_state] = h

    failures = list(db.scalars(
        select(FailureRecord).where(FailureRecord.transaction_id == txn.id)
    ))
    failed_states = {f.state_at_failure for f in failures}
    failure_note: dict[str, str] = {}
    for f in failures:
        failure_note.setdefault(
            f.state_at_failure,
            f"{f.failure_reason} ({f.failure_type})",
        )

    provider_for_kind = _provider_codes_by_kind(txn)
    current = TransactionState(txn.state)
    resume_idx = happy_index(TransactionState(txn.resume_state)) or 0

    nodes: list[TimelineNode] = []
    for i, state in enumerate(HAPPY_PATH):
        name = state.value
        hist = reached.get(name)

        if hist is not None:
            status = "COMPLETED"
        elif name in failed_states:
            status = "FAILED"
        elif i == resume_idx + 1 and current not in {
            TransactionState.CLOSED, TransactionState.REJECTED,
            TransactionState.CANCELLED, TransactionState.FAILED,
        }:
            status = "CURRENT"
        else:
            status = "PENDING"

        # A transaction that ended early leaves the rest of the path unreached.
        if status == "PENDING" and current in {
            TransactionState.CLOSED, TransactionState.REJECTED,
            TransactionState.CANCELLED, TransactionState.FAILED,
        }:
            status = "SKIPPED"

        note = failure_note.get(name)
        if hist is not None and note is None:
            note = hist.reason

        nodes.append(TimelineNode(
            state=name,
            label=STATE_LABELS.get(name, name),
            status=status,
            reached_at=hist.created_at if hist else None,
            provider_code=provider_for_kind.get(STATE_PROVIDER_KIND.get(name, "")),
            note=(note[:200] if note else None),
        ))
    return nodes


def _provider_codes_by_kind(txn: ServiceTransaction) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for p in txn.participants:
        if p.provider is not None:
            mapping.setdefault(p.provider.kind, p.provider.code)
    return mapping


def to_detail(txn: ServiceTransaction, db: Session) -> TransactionDetail:
    base = to_summary(txn).model_dump()

    component = (
        db.get(Component, txn.selected_component_id)
        if txn.selected_component_id else None
    )
    service_provider = (
        db.get(Provider, txn.selected_service_provider_id)
        if txn.selected_service_provider_id else None
    )
    supplier = (
        db.get(Provider, txn.selected_supplier_id) if txn.selected_supplier_id else None
    )

    excluded_providers = [
        p.code for p in db.scalars(
            select(Provider).where(Provider.id.in_(txn.excluded_provider_ids or [""]))
        )
    ] if txn.excluded_provider_ids else []
    excluded_components = [
        c.sku for c in db.scalars(
            select(Component).where(Component.id.in_(txn.excluded_component_ids or [""]))
        )
    ] if txn.excluded_component_ids else []

    failures = list(db.scalars(
        select(FailureRecord).where(FailureRecord.transaction_id == txn.id)
        .order_by(FailureRecord.created_at)
    ))
    provider_codes = {
        p.id: p.code for p in db.scalars(select(Provider))
    }
    audit_timeline = [
        {
            "id": a.id, "action": a.action, "target": a.target,
            "actor_id": a.actor_id, "actor_role": a.actor_role,
            "outcome": a.outcome, "details": a.details or {},
            "created_at": a.created_at,
        }
        for a in db.scalars(
            select(AuditEvent).where(AuditEvent.transaction_id == txn.id)
            .order_by(AuditEvent.created_at)
        )
    ]

    op_counts: dict[str, list[int]] = {}
    for op in txn.operations:
        if op.provider_id:
            entry = op_counts.setdefault(op.provider_id, [0, 0])
            entry[0] += 1
            if op.status in {"FAILED", "TIMED_OUT", "UNKNOWN"}:
                entry[1] += 1

    return TransactionDetail(
        **base,
        issue_description=txn.issue_description,
        raw_request_text=txn.raw_request_text,
        nlp_extraction=txn.nlp_extraction,
        purchase_evidence=txn.purchase_evidence,
        product_evidence=txn.product_evidence,
        warranty_evidence=txn.warranty_evidence,
        coverage_evidence=txn.coverage_evidence,
        model_code=(txn.product_evidence or {}).get("model_code"),
        required_component_type=txn.required_component_type,
        selected_component_sku=component.sku if component else None,
        service_provider_code=service_provider.code if service_provider else None,
        supplier_code=supplier.code if supplier else None,
        part_reservation_ref=txn.part_reservation_ref,
        service_booking_ref=txn.service_booking_ref,
        assigned_repair_person=txn.assigned_repair_person,
        diagnosis_notes=txn.diagnosis_notes,
        repair_notes=txn.repair_notes,
        verification_result=txn.verification_result,
        part_delivery_status=txn.part_delivery_status,
        part_eta=txn.part_eta,
        part_price=txn.part_price,
        supplier_decision=txn.supplier_decision,
        notifications=[
            {"id": n.id, "title": n.title, "body": n.body, "is_read": n.is_read,
             "created_at": n.created_at} for n in db.scalars(
                select(Notification).where(Notification.recipient_type == "CUSTOMER",
                    Notification.recipient_id == txn.customer_id,
                    Notification.transaction_id == txn.id).order_by(Notification.created_at.desc())
            )
        ],
        excluded_provider_codes=excluded_providers,
        excluded_component_skus=excluded_components,
        timeline=build_timeline(txn, db),
        state_history=[
            StateHistoryOut(
                from_state=h.from_state, to_state=h.to_state, reason=h.reason,
                actor=h.actor, context=h.context or {}, created_at=h.created_at,
            ) for h in txn.state_history
        ],
        operations=[
            OperationOut(
                id=op.id, sequence=op.sequence, operation_type=op.operation_type,
                status=op.status,
                provider_code=op.provider.code if op.provider else None,
                provider_name=op.provider.name if op.provider else None,
                provider_kind=op.provider.kind if op.provider else None,
                attempt_count=op.attempt_count, max_attempts=op.max_attempts,
                latency_ms=op.latency_ms,
                idempotency_key=op.idempotency_key,
                has_idempotency_protection=(
                    op.operation_type in {o.value for o in MUTATING_OPERATIONS}
                ),
                request_payload=op.request_payload or {},
                response_payload=op.response_payload,
                started_at=op.started_at, completed_at=op.completed_at,
                attempts=[
                    AttemptOut(
                        attempt_number=a.attempt_number, status=a.status,
                        http_status=a.http_status, latency_ms=a.latency_ms,
                        error_message=a.error_message,
                        started_at=a.started_at, finished_at=a.finished_at,
                    ) for a in op.attempts
                ],
            ) for op in txn.operations
        ],
        participants=[
            ParticipantOut(
                provider_code=p.provider.code, provider_name=p.provider.name,
                kind=p.provider.kind, role=p.role, joined_state=p.joined_state,
                status=p.status,
                operations_total=op_counts.get(p.provider_id, [0, 0])[0],
                operations_failed=op_counts.get(p.provider_id, [0, 0])[1],
            ) for p in txn.participants if p.provider is not None
        ],
        failures=[
            FailureOut(
                id=f.id, failure_type=f.failure_type, failure_reason=f.failure_reason,
                message=f.message, state_at_failure=f.state_at_failure,
                attempt_number=f.attempt_number,
                provider_code=provider_codes.get(f.provider_id or ""),
                created_at=f.created_at,
            ) for f in failures
        ],
        recovery_actions=[
            RecoveryOut(
                id=r.id, strategy=r.strategy, rule_id=r.rule_id,
                rationale=r.rationale, succeeded=r.succeeded,
                resumed_from_state=r.resumed_from_state, inputs=r.inputs or {},
                created_at=r.created_at,
            ) for r in txn.recovery_actions
        ],
        decisions=[
            DecisionOut(
                id=d.id, decision_type=d.decision_type, verdict=d.verdict,
                subject_label=d.subject_label, engine=d.engine,
                rule_version=d.rule_version, reasons=d.reasons or [],
                inputs=d.inputs or {}, scores=d.scores, created_at=d.created_at,
            ) for d in txn.decisions
        ],
        events=[
            EventOut(
                event_id=e.event_id, event_type=e.event_type,
                source_service=e.source_service, payload=e.payload or {},
                published=e.published, created_at=e.created_at,
            ) for e in txn.events
        ],
        audit_timeline=audit_timeline,
    )
