"""Service Transaction orchestrator.

Responsibilities
----------------
* execute one step of the workflow at a time, driven by persisted state
* record every operation, attempt, failure, decision and recovery action
* hand failures to the recovery engine and apply the strategy it returns
* never repeat a cross-organization operation that already succeeded

Why a persisted state machine rather than Temporal
--------------------------------------------------
Temporal was evaluated. It solves durable execution well, but it would own the
workflow while ServiceMesh still needs to own the domain model - the Service
Transaction, its policy decisions, its compatibility verdicts and its recovery
rationale all have to be queryable, auditable and explainable from ServiceMesh's
own database, because that is the research artefact. Running Temporal too would
mean two sources of truth about "where is this transaction" during the MVP.

So the durable state lives in PostgreSQL and the orchestrator is a resumable
step function over it. `drive()` is deliberately written so that it can be
called repeatedly, from anywhere, at any time, and will always do the right
next thing given the persisted state - which is precisely the contract a
Temporal activity would need. `docs/architecture.md` records the integration
boundary. This is the "simpler technically correct implementation first"
instruction applied honestly, not a claim that Temporal has no value.

Concurrency note
----------------
Provider I/O is async; database access is synchronous SQLAlchemy. At the scale
this system targets (local queries, network-bound provider calls) the blocking
DB calls inside the event loop are not the bottleneck, but this is a known
limitation recorded in the audit rather than hidden.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from servicemesh.adapters.base import (
    AdapterResult,
    OperationRequest,
    ProviderRef,
    get_adapter,
    load_builtin_adapters,
)
from servicemesh.core.config import get_settings
from servicemesh.core.enums import (
    MUTATING_OPERATIONS,
    EventType,
    FailureReason,
    FailureType,
    OperationStatus,
    OperationType,
    RecoveryStrategy,
    TransactionState,
)
from servicemesh.core.models import (
    DecisionRecord,
    FailureRecord,
    OperationAttempt,
    Provider,
    ProviderOperation,
    RecoveryAction,
    ServiceTransaction,
    TransactionParticipant,
    utcnow,
)
from servicemesh.engines.compatibility import CompatibilityEngine
from servicemesh.engines.policy import PolicyEngine
from servicemesh.engines.recovery import FailureContext, RecoveryEngine
from servicemesh.engines.selection import ProviderSelectionEngine
from servicemesh.events.bus import DomainEvent, event_bus
from servicemesh.orchestration.idempotency import check_idempotency
from servicemesh.orchestration.state_machine import StateMachine

logger = logging.getLogger("servicemesh.orchestrator")

load_builtin_adapters()


class StepOutcome:
    """Result of attempting one workflow step."""

    def __init__(
        self,
        ok: bool,
        *,
        failure: FailureContext | None = None,
        halt: bool = False,
        note: str = "",
    ) -> None:
        self.ok = ok
        self.failure = failure
        self.halt = halt          # stop driving, but not an error (WAITING)
        self.note = note


@dataclass
class DriveResult:
    transaction_id: str
    final_state: TransactionState
    steps_executed: int
    recoveries_applied: int
    halted_reason: str = ""


class Orchestrator:
    def __init__(self, db: Session, *, ml_provider_scorer=None) -> None:
        self.db = db
        self.sm = StateMachine(db)
        self.policy = PolicyEngine()
        self.compat = CompatibilityEngine(db)
        self.selection = ProviderSelectionEngine(db)
        self.recovery = RecoveryEngine()
        self.ml_provider_scorer = ml_provider_scorer

    # =====================================================================
    # Operation execution
    # =====================================================================

    async def execute_operation(
        self,
        txn: ServiceTransaction,
        operation: OperationType,
        provider: Provider,
        payload: dict,
        *,
        max_attempts: int | None = None,
    ) -> tuple[ProviderOperation, AdapterResult]:
        """Run one operation against one organization, recording everything."""
        settings = get_settings()
        max_attempts = max_attempts or settings.default_max_retries

        lookup = check_idempotency(self.db, txn.id, operation, provider.id, payload)

        # Already completed successfully -> reuse, do not call the network.
        if lookup.is_replay and lookup.existing is not None:
            op = lookup.existing
            logger.info(
                "txn=%s op=%s reusing completed operation %s (idempotency)",
                txn.reference, operation.value, op.id,
            )
            return op, AdapterResult(
                success=True, data=op.response_payload or {},
                status_code=200, latency_ms=op.latency_ms or 0.0,
            )

        op = lookup.existing
        if op is None:
            op = ProviderOperation(
                transaction_id=txn.id,
                provider_id=provider.id,
                sequence=self._next_sequence(txn),
                operation_type=operation.value,
                status=OperationStatus.IN_PROGRESS.value,
                idempotency_key=lookup.key,
                request_payload=_safe(payload),
                max_attempts=max_attempts,
                started_at=utcnow(),
            )
            self.db.add(op)
            self.db.flush()
        else:
            op.status = OperationStatus.IN_PROGRESS.value

        self._ensure_participant(txn, provider, operation)

        op.attempt_count += 1
        attempt = OperationAttempt(
            operation_id=op.id,
            attempt_number=op.attempt_count,
            status=OperationStatus.IN_PROGRESS.value,
            request_snapshot=_safe(payload),
        )
        self.db.add(attempt)
        self.db.flush()

        adapter = get_adapter(provider.adapter_key)
        result = await adapter.execute(OperationRequest(
            operation=operation,
            provider=_ref(provider),
            payload=payload,
            correlation_id=txn.correlation_id,
            idempotency_key=lookup.wire_key,
        ))

        # ---- record the attempt -----------------------------------------
        attempt.latency_ms = result.latency_ms
        attempt.http_status = result.status_code
        attempt.response_snapshot = _safe(result.data) if result.success else None
        attempt.error_message = result.error_message
        attempt.finished_at = utcnow()
        attempt.status = (
            OperationStatus.SUCCEEDED.value if result.success
            else OperationStatus.FAILED.value
        )

        op.latency_ms = result.latency_ms
        op.response_payload = _safe(result.data)

        if result.success:
            op.status = OperationStatus.SUCCEEDED.value
            op.completed_at = utcnow()
        else:
            # A timeout on a *mutating* operation is an UNKNOWN outcome, not a
            # plain failure: the side effect may already exist at the
            # organization. Read-only timeouts carry no such ambiguity.
            if (result.failure_reason is FailureReason.TIMEOUT
                    and operation in MUTATING_OPERATIONS):
                op.status = OperationStatus.UNKNOWN.value
                attempt.status = OperationStatus.UNKNOWN.value
            else:
                op.status = OperationStatus.FAILED.value

        self._update_provider_stats(provider, result)
        self.db.flush()
        return op, result

    def _next_sequence(self, txn: ServiceTransaction) -> int:
        existing = self.db.scalar(
            select(ProviderOperation.sequence)
            .where(ProviderOperation.transaction_id == txn.id)
            .order_by(ProviderOperation.sequence.desc())
            .limit(1)
        )
        return (existing or 0) + 1

    def _ensure_participant(
        self, txn: ServiceTransaction, provider: Provider, operation: OperationType
    ) -> None:
        exists = self.db.scalar(
            select(TransactionParticipant).where(
                TransactionParticipant.transaction_id == txn.id,
                TransactionParticipant.provider_id == provider.id,
            )
        )
        if exists is None:
            self.db.add(TransactionParticipant(
                transaction_id=txn.id, provider_id=provider.id,
                role=provider.kind, joined_state=txn.state,
            ))
            self.db.flush()

    def _update_provider_stats(self, provider: Provider, result: AdapterResult) -> None:
        """Reliability figures are ServiceMesh's own observations."""
        provider.total_operations += 1
        provider.total_latency_ms += result.latency_ms
        if result.success:
            provider.successful_operations += 1
        else:
            provider.failed_operations += 1

    # =====================================================================
    # Failure recording + recovery
    # =====================================================================

    def record_failure(
        self,
        txn: ServiceTransaction,
        op: ProviderOperation | None,
        provider: Provider | None,
        *,
        failure_type: FailureType,
        failure_reason: FailureReason,
        message: str,
    ) -> FailureRecord:
        rec = FailureRecord(
            transaction_id=txn.id,
            operation_id=op.id if op else None,
            provider_id=provider.id if provider else None,
            failure_type=failure_type.value,
            failure_reason=failure_reason.value,
            message=message[:2000],
            state_at_failure=txn.state,
            attempt_number=op.attempt_count if op else 1,
            details={"provider_code": provider.code if provider else None},
        )
        self.db.add(rec)
        self.db.flush()

        event_bus.publish(self.db, DomainEvent(
            event_type=EventType.OPERATION_FAILED,
            transaction_id=txn.id, correlation_id=txn.correlation_id,
            payload={
                "operation": op.operation_type if op else None,
                "provider_code": provider.code if provider else None,
                "failure_type": failure_type.value,
                "failure_reason": failure_reason.value,
                "message": message[:500],
            },
        ))
        return rec

    def build_failure_context(
        self,
        txn: ServiceTransaction,
        op: ProviderOperation | None,
        provider: Provider | None,
        *,
        failure_type: FailureType,
        failure_reason: FailureReason,
        message: str,
        operation_type: OperationType,
        provider_declared_terminal: bool = False,
    ) -> FailureContext:
        return FailureContext(
            failure_type=failure_type,
            failure_reason=failure_reason,
            operation_type=operation_type,
            state=TransactionState(txn.state),
            attempt_number=op.attempt_count if op else 1,
            max_attempts=op.max_attempts if op else get_settings().default_max_retries,
            message=message,
            provider_id=provider.id if provider else None,
            provider_code=provider.code if provider else None,
            operation_id=op.id if op else None,
            idempotency_key=op.idempotency_key if op else None,
            alternative_providers_available=self._has_alternative_provider(txn, provider),
            alternative_components_available=self._has_alternative_component(txn),
            has_uncompensated_side_effects=self._has_side_effects(txn),
            provider_declared_terminal=provider_declared_terminal,
            provider_success_rate=provider.success_rate if provider else 1.0,
            total_retries_on_transaction=txn.retry_total,
        )

    def _has_alternative_provider(
        self, txn: ServiceTransaction, failing: Provider | None
    ) -> bool:
        if failing is None:
            return False
        excluded = list(txn.excluded_provider_ids or []) + [failing.id]
        result = self.selection.select(
            failing.kind,
            customer_region=txn.customer.region,
            manufacturer_code=self._manufacturer_code(txn),
            exclude_provider_ids=excluded,
            customer_lat=txn.customer.latitude,
            customer_lon=txn.customer.longitude,
        )
        return result.found

    def _has_alternative_component(self, txn: ServiceTransaction) -> bool:
        if not txn.product_model_id or not txn.required_component_type:
            return False
        excluded = list(txn.excluded_component_ids or [])
        if txn.selected_component_id:
            excluded.append(txn.selected_component_id)
        candidates = self.compat.find_compatible(
            txn.product_model_id, txn.required_component_type,
            context=self._compat_context(txn), exclude_component_ids=excluded,
        )
        return bool(candidates)

    def _has_side_effects(self, txn: ServiceTransaction) -> bool:
        return bool(txn.part_reservation_ref or txn.service_booking_ref)

    def _manufacturer_code(self, txn: ServiceTransaction) -> str | None:
        return (txn.product_evidence or {}).get("manufacturer_code")

    def _compat_context(self, txn: ServiceTransaction) -> dict:
        ev = txn.product_evidence or {}
        return {
            "bios_version": ev.get("bios_version"),
            "region": txn.customer.region,
            "approved_component_skus": ev.get("approved_component_skus"),
        }

    async def apply_recovery(
        self, txn: ServiceTransaction, ctx: FailureContext, failure: FailureRecord
    ) -> bool:
        """Decide and apply a recovery strategy. Returns True to keep driving."""
        decision = self.recovery.decide(ctx)

        action = RecoveryAction(
            transaction_id=txn.id, failure_id=failure.id,
            strategy=decision.strategy.value, rationale=decision.rationale,
            rule_id=decision.rule_id, inputs=decision.inputs,
            resumed_from_state=decision.resume_from.value if decision.resume_from else None,
        )
        self.db.add(action)
        self.db.flush()

        event_bus.publish(self.db, DomainEvent(
            event_type=EventType.RECOVERY_STARTED,
            transaction_id=txn.id, correlation_id=txn.correlation_id,
            payload={
                "strategy": decision.strategy.value, "rule_id": decision.rule_id,
                "rationale": decision.rationale,
            },
        ))

        logger.info(
            "txn=%s recovery=%s rule=%s", txn.reference,
            decision.strategy.value, decision.rule_id,
        )

        if decision.exclude_provider and ctx.provider_id:
            txn.excluded_provider_ids = list(txn.excluded_provider_ids or []) + [
                ctx.provider_id
            ]
        if decision.exclude_component and txn.selected_component_id:
            txn.excluded_component_ids = list(txn.excluded_component_ids or []) + [
                txn.selected_component_id
            ]
            txn.selected_component_id = None

        match decision.strategy:
            case RecoveryStrategy.RETRY | RecoveryStrategy.RETRY_WITH_BACKOFF:
                txn.retry_total += 1
                self.sm.transition(
                    txn, TransactionState.RETRYING, reason=decision.rationale,
                    actor="RECOVERY_ENGINE",
                    context={"rule_id": decision.rule_id,
                             "delay_seconds": decision.delay_seconds},
                )
                event_bus.publish(self.db, DomainEvent(
                    event_type=EventType.RETRY_STARTED,
                    transaction_id=txn.id, correlation_id=txn.correlation_id,
                    payload={"attempt": ctx.attempt_number + 1,
                             "delay_seconds": decision.delay_seconds},
                ))
                if decision.delay_seconds > 0:
                    await asyncio.sleep(decision.delay_seconds)
                self._resume_to(txn, decision.resume_from, decision.rationale)
                action.succeeded = True
                return True

            case RecoveryStrategy.RECONCILE_VIA_IDEMPOTENCY:
                ok = await self._reconcile(txn, ctx, decision.rationale)
                action.succeeded = ok
                if ok:
                    return True
                self.sm.transition(
                    txn, TransactionState.ESCALATED,
                    reason="reconciliation could not determine the outcome",
                    actor="RECOVERY_ENGINE",
                )
                return False

            case RecoveryStrategy.FALLBACK_PROVIDER | RecoveryStrategy.ALTERNATIVE_COMPONENT:
                txn.retry_total += 1
                self.sm.transition(
                    txn, TransactionState.RETRYING, reason=decision.rationale,
                    actor="RECOVERY_ENGINE", context={"rule_id": decision.rule_id},
                )
                self._resume_to(txn, decision.resume_from, decision.rationale)
                action.succeeded = True
                return True

            case RecoveryStrategy.COMPENSATE:
                self.sm.transition(
                    txn, TransactionState.COMPENSATING, reason=decision.rationale,
                    actor="RECOVERY_ENGINE",
                )
                await self.compensate(txn, reason=decision.rationale)
                self.sm.transition(
                    txn, TransactionState.ESCALATED,
                    reason="compensation complete; operator review required",
                    actor="RECOVERY_ENGINE", force=True,
                )
                action.succeeded = True
                return False

            case RecoveryStrategy.ESCALATE:
                self.sm.transition(
                    txn, TransactionState.ESCALATED, reason=decision.rationale,
                    actor="RECOVERY_ENGINE", context={"rule_id": decision.rule_id},
                )
                action.succeeded = True
                return False

            case RecoveryStrategy.TERMINATE:
                txn.outcome = "FAILED"
                txn.outcome_reason = decision.rationale
                self.sm.transition(
                    txn, TransactionState.FAILED, reason=decision.rationale,
                    actor="RECOVERY_ENGINE",
                )
                action.succeeded = True
                return False

            case RecoveryStrategy.WAIT:
                self.sm.transition(
                    txn, TransactionState.WAITING, reason=decision.rationale,
                    actor="RECOVERY_ENGINE",
                )
                action.succeeded = True
                return False

        return False

    def _resume_to(
        self, txn: ServiceTransaction, target: TransactionState | None, reason: str
    ) -> None:
        """Put the transaction back on the happy path at a safe point.

        Never moves past resume_state - completed cross-organization work is
        preserved, which is the entire justification for tracking resume_state
        separately from state.
        """
        from servicemesh.orchestration.state_machine import happy_index

        resume = TransactionState(txn.resume_state)
        if target is not None:
            ti, ri = happy_index(target), happy_index(resume)
            if ti is not None and ri is not None and ti < ri:
                resume = target
        self.sm.transition(
            txn, resume, reason=f"resuming from {resume.value}: {reason}",
            actor="RECOVERY_ENGINE", emit_event=False, force=True,
        )

    async def _reconcile(
        self, txn: ServiceTransaction, ctx: FailureContext, rationale: str
    ) -> bool:
        """Ask the organization whether the uncertain side effect exists."""
        if not ctx.provider_id or not ctx.idempotency_key:
            return False
        provider = self.db.get(Provider, ctx.provider_id)
        if provider is None:
            return False

        op, result = await self.execute_operation(
            txn, OperationType.GET_OPERATION_STATUS, provider,
            {"idempotency_key": ctx.idempotency_key},
        )
        if not result.success:
            return False

        found = bool(result.data.get("found"))
        if not found:
            # The side effect never landed. Safe to redo from the resume point.
            self._resume_to(txn, ctx.state, "reconciliation: side effect never committed")
            return True

        # The side effect DID land. Adopt it instead of repeating it.
        if result.data.get("reservation_ref"):
            txn.part_reservation_ref = result.data["reservation_ref"]
            self._record_decision(
                txn, "RECONCILIATION", "CONFIRMED",
                subject_label=result.data["reservation_ref"],
                engine="recovery_engine",
                reasons=[
                    "reservation was committed at the supplier despite the timeout",
                    "adopting the existing reservation; no second reservation made",
                ],
            )
            self.sm.transition(
                txn, TransactionState.PART_CONFIRMED,
                reason="reconciled: reservation confirmed via idempotency key",
                actor="RECOVERY_ENGINE", force=True,
            )
            return True

        if result.data.get("booking_ref"):
            txn.service_booking_ref = result.data["booking_ref"]
            self._record_decision(
                txn, "RECONCILIATION", "CONFIRMED",
                subject_label=result.data["booking_ref"],
                engine="recovery_engine",
                reasons=["booking was committed at the service centre despite the timeout"],
            )
            self.sm.transition(
                txn, TransactionState.REPAIR_SCHEDULED,
                reason="reconciled: booking confirmed via idempotency key",
                actor="RECOVERY_ENGINE", force=True,
            )
            return True

        return False

    async def compensate(self, txn: ServiceTransaction, *, reason: str) -> list[str]:
        """Undo committed side effects, most recent first (Saga compensation)."""
        undone: list[str] = []

        if txn.service_booking_ref and txn.selected_service_provider_id:
            provider = self.db.get(Provider, txn.selected_service_provider_id)
            if provider is not None:
                _, result = await self.execute_operation(
                    txn, OperationType.CANCEL_SERVICE, provider,
                    {"booking_ref": txn.service_booking_ref},
                )
                if result.success:
                    undone.append(f"cancelled booking {txn.service_booking_ref}")
                    txn.service_booking_ref = None

        if txn.part_reservation_ref and txn.selected_supplier_id:
            provider = self.db.get(Provider, txn.selected_supplier_id)
            if provider is not None:
                _, result = await self.execute_operation(
                    txn, OperationType.RELEASE_PART, provider,
                    {"reservation_ref": txn.part_reservation_ref},
                )
                if result.success:
                    undone.append(f"released reservation {txn.part_reservation_ref}")
                    txn.part_reservation_ref = None

        event_bus.publish(self.db, DomainEvent(
            event_type=EventType.COMPENSATION_EXECUTED,
            transaction_id=txn.id, correlation_id=txn.correlation_id,
            payload={"reason": reason, "actions": undone},
        ))
        self.db.flush()
        return undone

    # =====================================================================
    # Decision recording
    # =====================================================================

    def _record_decision(
        self,
        txn: ServiceTransaction,
        decision_type: str,
        verdict: str,
        *,
        engine: str,
        reasons: list[str],
        subject_id: str | None = None,
        subject_label: str | None = None,
        inputs: dict | None = None,
        scores: dict | None = None,
        rule_version: str = "v1",
    ) -> DecisionRecord:
        rec = DecisionRecord(
            transaction_id=txn.id, decision_type=decision_type, verdict=verdict,
            subject_id=subject_id, subject_label=subject_label, engine=engine,
            rule_version=rule_version, reasons=reasons, inputs=_safe(inputs or {}),
            scores=_safe(scores) if scores else None,
        )
        self.db.add(rec)
        self.db.flush()
        return rec


def _ref(provider: Provider) -> ProviderRef:
    return ProviderRef(
        id=provider.id, code=provider.code, kind=provider.kind,
        base_url=provider.base_url, adapter_key=provider.adapter_key,
        region=provider.region, metadata=provider.metadata_json or {},
    )


def _safe(value):
    """Make a payload JSON-serialisable for storage."""
    import json

    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return json.loads(json.dumps(value, default=str))
