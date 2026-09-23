"""Service Transaction state machine.

This is the heart of the project. A Service Transaction is a business-level
distributed transaction spanning five independently governed organizations. No
ACID transaction can span them, so correctness comes from this state machine
being (a) persisted on every change, (b) explicit about which transitions are
legal, and (c) able to distinguish "where am I now" from "where is it safe to
resume from".

resume_state vs state
---------------------
`state` is where the transaction is *right now* - possibly RETRYING, TIMEOUT or
ESCALATED. `resume_state` is the last happy-path milestone that completed
safely. When recovery restarts work, it resumes from `resume_state`, which is
why a supplier timeout does not re-verify the purchase, re-verify the product
and re-check the warranty. Every already-completed cross-organization operation
stays completed.

This separation is what the research hypotheses about restart avoidance and
manual-intervention reduction actually rest on, so it is enforced here rather
than left to orchestrator discipline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from servicemesh.core.enums import (
    HAPPY_PATH,
    TERMINAL_STATES,
    EventType,
    TransactionState,
)
from servicemesh.core.models import ServiceTransaction, TransactionStateHistory

#: Control states reachable from (almost) any working state.
CONTROL_STATES: frozenset[TransactionState] = frozenset({
    TransactionState.FAILED,
    TransactionState.TIMEOUT,
    TransactionState.RETRYING,
    TransactionState.WAITING,
    TransactionState.ESCALATED,
    TransactionState.COMPENSATING,
    TransactionState.CANCELLED,
    TransactionState.REJECTED,
})

#: Maps each happy-path state to the event announcing arrival there.
STATE_EVENT: dict[TransactionState, EventType] = {
    TransactionState.CREATED: EventType.TRANSACTION_CREATED,
    TransactionState.PURCHASE_VERIFIED: EventType.PURCHASE_VERIFIED,
    TransactionState.PRODUCT_VERIFIED: EventType.PRODUCT_VERIFIED,
    TransactionState.WARRANTY_VERIFIED: EventType.WARRANTY_VERIFIED,
    TransactionState.COVERAGE_CHECKED: EventType.COVERAGE_CHECKED,
    TransactionState.PROVIDER_SELECTED: EventType.PROVIDER_SELECTED,
    TransactionState.COMPONENT_VALIDATED: EventType.COMPONENT_VALIDATED,
    TransactionState.PART_REQUESTED: EventType.PART_REQUESTED,
    TransactionState.PART_CONFIRMED: EventType.PART_RESERVED,
    TransactionState.REPAIR_SCHEDULED: EventType.SERVICE_SCHEDULED,
    TransactionState.REPAIR_IN_PROGRESS: EventType.REPAIR_STARTED,
    TransactionState.REPAIR_COMPLETED: EventType.REPAIR_COMPLETED,
    TransactionState.CLOSED: EventType.TRANSACTION_CLOSED,
    TransactionState.ESCALATED: EventType.TRANSACTION_ESCALATED,
    TransactionState.REJECTED: EventType.TRANSACTION_REJECTED,
}


class IllegalTransition(Exception):
    """Raised when code attempts a transition the model forbids."""


@dataclass(frozen=True)
class TransitionResult:
    transaction_id: str
    from_state: TransactionState
    to_state: TransactionState
    resume_state: TransactionState


def happy_index(state: TransactionState) -> int | None:
    try:
        return HAPPY_PATH.index(state)
    except ValueError:
        return None


def next_happy_state(state: TransactionState) -> TransactionState | None:
    """The state that follows `state` on the normal path."""
    idx = happy_index(state)
    if idx is None or idx + 1 >= len(HAPPY_PATH):
        return None
    return HAPPY_PATH[idx + 1]


def is_terminal(state: TransactionState) -> bool:
    return state in TERMINAL_STATES


def allowed_transitions(state: TransactionState) -> set[TransactionState]:
    """Legal successors of `state`."""
    if is_terminal(state):
        # A closed/rejected/cancelled transaction is immutable. FAILED is the
        # one terminal state an operator may reopen, via ESCALATED.
        if state is TransactionState.FAILED:
            return {TransactionState.ESCALATED, TransactionState.COMPENSATING}
        return set()

    allowed: set[TransactionState] = set(CONTROL_STATES)

    nxt = next_happy_state(state)
    if nxt is not None:
        allowed.add(nxt)

    if state in CONTROL_STATES:
        # Recovering from a control state means re-entering the happy path.
        # Any happy-path state is reachable because the orchestrator resumes
        # from resume_state, which may be several steps back.
        allowed |= set(HAPPY_PATH)

    return allowed


def can_transition(frm: TransactionState, to: TransactionState) -> bool:
    return to in allowed_transitions(frm)


class StateMachine:
    """Applies and records transitions. All state changes go through here."""

    def __init__(self, db: Session, bus=None) -> None:
        self.db = db
        # Imported lazily so the state machine can be unit-tested without the
        # event subsystem.
        if bus is None:
            from servicemesh.events.bus import event_bus

            bus = event_bus
        self.bus = bus

    def transition(
        self,
        txn: ServiceTransaction,
        to_state: TransactionState,
        *,
        reason: str,
        actor: str = "ORCHESTRATOR",
        context: dict | None = None,
        emit_event: bool = True,
        force: bool = False,
    ) -> TransitionResult:
        current = TransactionState(txn.state)

        if not force and not can_transition(current, to_state):
            raise IllegalTransition(
                f"transaction {txn.reference}: {current.value} -> {to_state.value} "
                f"is not a legal transition"
            )

        txn.previous_state = current.value
        txn.state = to_state.value

        # Advance the safe resume point only when moving forward on the happy
        # path. Control states never move it backwards - that is the whole
        # point of keeping it separate.
        idx_new = happy_index(to_state)
        idx_resume = happy_index(TransactionState(txn.resume_state))
        if idx_new is not None and (idx_resume is None or idx_new > idx_resume):
            txn.resume_state = to_state.value

        if to_state in TERMINAL_STATES and txn.closed_at is None:
            txn.closed_at = datetime.now(UTC)

        if to_state is TransactionState.ESCALATED:
            txn.requires_manual_intervention = True
            txn.manual_interventions += 1

        self.db.add(TransactionStateHistory(
            transaction_id=txn.id,
            from_state=current.value,
            to_state=to_state.value,
            reason=reason,
            actor=actor,
            context=context or {},
        ))
        self.db.flush()

        if emit_event:
            self._emit(txn, current, to_state, reason, context or {})

        return TransitionResult(
            transaction_id=txn.id, from_state=current,
            to_state=to_state, resume_state=TransactionState(txn.resume_state),
        )

    def _emit(
        self,
        txn: ServiceTransaction,
        frm: TransactionState,
        to: TransactionState,
        reason: str,
        context: dict,
    ) -> None:
        from servicemesh.events.bus import DomainEvent

        event_type = STATE_EVENT.get(to, EventType.STATE_CHANGED)
        self.bus.publish(self.db, DomainEvent(
            event_type=event_type,
            transaction_id=txn.id,
            correlation_id=txn.correlation_id,
            payload={
                "reference": txn.reference,
                "from_state": frm.value,
                "to_state": to.value,
                "resume_state": txn.resume_state,
                "reason": reason,
                **context,
            },
        ))

    # ------------------------------------------------------------ helpers
    def advance(
        self, txn: ServiceTransaction, *, reason: str, context: dict | None = None
    ) -> TransitionResult:
        """Move one step along the happy path."""
        nxt = next_happy_state(TransactionState(txn.state))
        if nxt is None:
            raise IllegalTransition(
                f"transaction {txn.reference} is at {txn.state}; no next happy state"
            )
        return self.transition(txn, nxt, reason=reason, context=context)

    def resume_point(self, txn: ServiceTransaction) -> TransactionState:
        return TransactionState(txn.resume_state)

    def remaining_steps(self, txn: ServiceTransaction) -> list[TransactionState]:
        """Happy-path states still to be reached, from the resume point."""
        idx = happy_index(TransactionState(txn.resume_state))
        if idx is None:
            return []
        return list(HAPPY_PATH[idx + 1:])

    def progress_percent(self, txn: ServiceTransaction) -> float:
        idx = happy_index(TransactionState(txn.resume_state)) or 0
        return round(100.0 * idx / (len(HAPPY_PATH) - 1), 1)
