"""Recovery engine - the failure/recovery matrix, in code.

Input : a FailureContext (what failed, where, how many times, what is left)
Output: a RecoveryDecision (strategy, rationale, rule id)

Design commitments
------------------
1. **Deterministic first.** Rules are ordered and explicit. Given the same
   context the engine always returns the same decision, which is what makes
   the behaviour testable and the audit trail meaningful. An ML hook exists
   (`ml_advisor`) but it may only *reorder preferences among already-legal
   strategies*; it cannot invent one, and it cannot authorise retrying a
   permanent failure.

2. **Never retry a permanent failure.** This is the rule most integration code
   gets wrong. A 404, a business rejection, an expired warranty and an
   unauthorized partner will all return the identical answer forever. Retrying
   them wastes time, inflates load on the other organization, and delays
   telling the customer the truth.

3. **Never guess about an unknown outcome.** If a mutating call timed out, the
   side effect may or may not exist. The only correct move is to ask the
   organization, keyed by the idempotency key we already hold.

The matrix
----------
  transient + attempts left                 -> RETRY_WITH_BACKOFF
  transient + attempts exhausted + alt.     -> FALLBACK_PROVIDER
  transient + attempts exhausted + no alt.  -> ESCALATE
  unknown (mutating timeout)                -> RECONCILE_VIA_IDEMPOTENCY
  permanent + terminal business rejection   -> TERMINATE
  permanent + provider-scoped rejection     -> FALLBACK_PROVIDER
  permanent + component-scoped rejection    -> ALTERNATIVE_COMPONENT
  permanent + no alternative                -> ESCALATE
  any + side effects needing undo           -> COMPENSATE
"""

from __future__ import annotations

from dataclasses import dataclass, field

from servicemesh.core.config import get_settings
from servicemesh.core.enums import (
    MUTATING_OPERATIONS,
    FailureReason,
    FailureType,
    OperationType,
    RecoveryStrategy,
    TransactionState,
)

RECOVERY_VERSION = "recovery-v1"

#: Reasons that are permanent *for the whole transaction* - no provider or
#: component substitution can rescue them.
TERMINAL_REASONS: frozenset[FailureReason] = frozenset({
    FailureReason.NOT_FOUND,
    FailureReason.POLICY_VIOLATION,
})

#: Reasons that are permanent *for this provider only* - try another.
PROVIDER_SCOPED_REASONS: frozenset[FailureReason] = frozenset({
    FailureReason.OUT_OF_STOCK,
    FailureReason.BUSINESS_REJECTION,
    FailureReason.UNAUTHORIZED,
})

#: Reasons that are permanent *for this component only* - try another part.
COMPONENT_SCOPED_REASONS: frozenset[FailureReason] = frozenset({
    FailureReason.INCOMPATIBLE_COMPONENT,
})


@dataclass
class FailureContext:
    """Everything the engine is allowed to consider."""

    failure_type: FailureType
    failure_reason: FailureReason
    operation_type: OperationType
    state: TransactionState
    attempt_number: int
    max_attempts: int
    message: str = ""
    provider_id: str | None = None
    provider_code: str | None = None
    #: The ProviderOperation this failure belongs to. Carried explicitly so
    #: retry accounting always reads the attempt count of the operation that
    #: actually failed.
    operation_id: str | None = None
    idempotency_key: str | None = None
    #: Are there untried providers of the needed kind?
    alternative_providers_available: bool = False
    #: Are there untried compatible components?
    alternative_components_available: bool = False
    #: Side effects already committed that would need undoing
    has_uncompensated_side_effects: bool = False
    #: Business rejection that the provider explicitly marked terminal
    provider_declared_terminal: bool = False
    #: Observed reliability of the failing provider, 0..1
    provider_success_rate: float = 1.0
    total_retries_on_transaction: int = 0

    @property
    def attempts_remaining(self) -> int:
        return max(0, self.max_attempts - self.attempt_number)

    @property
    def is_mutating(self) -> bool:
        return self.operation_type in MUTATING_OPERATIONS


@dataclass
class RecoveryDecision:
    strategy: RecoveryStrategy
    rule_id: str
    rationale: str
    delay_seconds: float = 0.0
    #: When the strategy is a retry, resume work from here
    resume_from: TransactionState | None = None
    exclude_provider: bool = False
    exclude_component: bool = False
    inputs: dict = field(default_factory=dict)
    version: str = RECOVERY_VERSION
    ml_advisory: dict | None = None


def backoff_delay(attempt: int) -> float:
    """Exponential backoff, capped, scaled by a test-friendly multiplier."""
    s = get_settings()
    delay = s.retry_base_delay_seconds * (2 ** max(0, attempt - 1))
    return round(min(delay, s.retry_max_delay_seconds) * s.retry_delay_multiplier, 4)


class RecoveryEngine:
    """Maps failure context onto a recovery strategy."""

    def __init__(self, ml_advisor=None) -> None:
        #: Optional callable(FailureContext) -> dict. Advisory only.
        self.ml_advisor = ml_advisor

    def decide(self, ctx: FailureContext) -> RecoveryDecision:
        decision = self._decide_deterministic(ctx)

        # ML may annotate but never override. Recorded for research comparison.
        if self.ml_advisor is not None:
            try:
                advisory = self.ml_advisor(ctx)
                if advisory:
                    decision.ml_advisory = advisory
            except Exception:  # noqa: BLE001 - advisory must never break recovery
                decision.ml_advisory = None
        return decision

    # ------------------------------------------------------------ rules
    def _decide_deterministic(self, ctx: FailureContext) -> RecoveryDecision:
        base_inputs = {
            "failure_type": ctx.failure_type.value,
            "failure_reason": ctx.failure_reason.value,
            "operation_type": ctx.operation_type.value,
            "state": ctx.state.value,
            "attempt_number": ctx.attempt_number,
            "max_attempts": ctx.max_attempts,
            "attempts_remaining": ctx.attempts_remaining,
            "provider_id": ctx.provider_id,
            "provider_code": ctx.provider_code,
            "alternative_providers_available": ctx.alternative_providers_available,
            "alternative_components_available": ctx.alternative_components_available,
            "provider_success_rate": round(ctx.provider_success_rate, 4),
        }

        # --- R1: unknown outcome on a mutating call --------------------
        # Checked first: until we know whether the side effect happened, every
        # other strategy risks either duplication or stranding the customer.
        if ctx.failure_type is FailureType.UNKNOWN or (
            ctx.failure_reason is FailureReason.TIMEOUT and ctx.is_mutating
        ):
            if ctx.idempotency_key:
                return RecoveryDecision(
                    strategy=RecoveryStrategy.RECONCILE_VIA_IDEMPOTENCY,
                    rule_id="R1_UNKNOWN_OUTCOME_RECONCILE",
                    rationale=(
                        f"{ctx.operation_type.value} did not return a result, so the "
                        f"side effect at {ctx.provider_code} may or may not exist. "
                        f"Querying the organization by idempotency key before taking "
                        f"any further action."
                    ),
                    resume_from=ctx.state,
                    inputs=base_inputs,
                )
            return RecoveryDecision(
                strategy=RecoveryStrategy.ESCALATE,
                rule_id="R1B_UNKNOWN_OUTCOME_NO_KEY",
                rationale=(
                    "outcome is unknown and no idempotency key is available to "
                    "reconcile against; automated recovery is unsafe"
                ),
                inputs=base_inputs,
            )

        # --- R2: permanent, terminal for the whole transaction ---------
        if ctx.failure_type is FailureType.PERMANENT:
            if ctx.provider_declared_terminal or ctx.failure_reason in TERMINAL_REASONS:
                return RecoveryDecision(
                    strategy=(
                        RecoveryStrategy.COMPENSATE
                        if ctx.has_uncompensated_side_effects
                        else RecoveryStrategy.TERMINATE
                    ),
                    rule_id="R2_PERMANENT_TERMINAL",
                    rationale=(
                        f"{ctx.failure_reason.value} is a definitive business answer "
                        f"({ctx.message or 'no further detail'}). Retrying cannot "
                        f"change it."
                        + (" Undoing committed side effects first."
                           if ctx.has_uncompensated_side_effects else "")
                    ),
                    inputs=base_inputs,
                )

            # --- R3: permanent but scoped to this component ------------
            if ctx.failure_reason in COMPONENT_SCOPED_REASONS:
                if ctx.alternative_components_available:
                    return RecoveryDecision(
                        strategy=RecoveryStrategy.ALTERNATIVE_COMPONENT,
                        rule_id="R3_COMPONENT_SCOPED_SUBSTITUTE",
                        rationale=(
                            "component was rejected by the compatibility engine; "
                            "another compatible component is available and will be "
                            "evaluated"
                        ),
                        exclude_component=True,
                        resume_from=TransactionState.PROVIDER_SELECTED,
                        inputs=base_inputs,
                    )
                return RecoveryDecision(
                    strategy=RecoveryStrategy.ESCALATE,
                    rule_id="R3B_COMPONENT_SCOPED_NO_ALTERNATIVE",
                    rationale=(
                        "no other compatible component exists for this model; a "
                        "human must decide whether to source a non-catalogued part"
                    ),
                    exclude_component=True,
                    inputs=base_inputs,
                )

            # --- R4: permanent but scoped to this provider -------------
            if ctx.failure_reason in PROVIDER_SCOPED_REASONS:
                if ctx.alternative_providers_available:
                    return RecoveryDecision(
                        strategy=RecoveryStrategy.FALLBACK_PROVIDER,
                        rule_id="R4_PROVIDER_SCOPED_FALLBACK",
                        rationale=(
                            f"{ctx.provider_code} gave a definitive negative "
                            f"({ctx.failure_reason.value}); selecting the next "
                            f"eligible provider instead of retrying"
                        ),
                        exclude_provider=True,
                        resume_from=_fallback_resume_state(ctx),
                        inputs=base_inputs,
                    )
                return RecoveryDecision(
                    strategy=RecoveryStrategy.ESCALATE,
                    rule_id="R4B_PROVIDER_SCOPED_NO_ALTERNATIVE",
                    rationale=(
                        f"{ctx.provider_code} cannot fulfil this operation and no "
                        f"alternative provider satisfies the constraints"
                    ),
                    exclude_provider=True,
                    inputs=base_inputs,
                )

            # --- R5: permanent, unclassified ---------------------------
            return RecoveryDecision(
                strategy=RecoveryStrategy.ESCALATE,
                rule_id="R5_PERMANENT_UNCLASSIFIED",
                rationale=(
                    f"permanent failure {ctx.failure_reason.value} has no automated "
                    f"recovery path defined; routing to an operator"
                ),
                inputs=base_inputs,
            )

        # --- R6: transient with attempts remaining ---------------------
        if ctx.attempts_remaining > 0:
            return RecoveryDecision(
                strategy=RecoveryStrategy.RETRY_WITH_BACKOFF,
                rule_id="R6_TRANSIENT_RETRY",
                rationale=(
                    f"{ctx.failure_reason.value} at {ctx.provider_code} is transient; "
                    f"attempt {ctx.attempt_number} of {ctx.max_attempts}, retrying "
                    f"after backoff"
                ),
                delay_seconds=backoff_delay(ctx.attempt_number),
                resume_from=ctx.state,
                inputs=base_inputs,
            )

        # --- R7: transient, attempts exhausted -------------------------
        if ctx.alternative_providers_available:
            return RecoveryDecision(
                strategy=RecoveryStrategy.FALLBACK_PROVIDER,
                rule_id="R7_RETRIES_EXHAUSTED_FALLBACK",
                rationale=(
                    f"{ctx.provider_code} failed {ctx.attempt_number} consecutive "
                    f"attempts (observed success rate "
                    f"{ctx.provider_success_rate:.0%}); switching to an alternative "
                    f"provider rather than continuing to retry"
                ),
                exclude_provider=True,
                resume_from=_fallback_resume_state(ctx),
                inputs=base_inputs,
            )

        # --- R8: nothing left to try -----------------------------------
        return RecoveryDecision(
            strategy=(
                RecoveryStrategy.COMPENSATE
                if ctx.has_uncompensated_side_effects
                else RecoveryStrategy.ESCALATE
            ),
            rule_id="R8_EXHAUSTED_ESCALATE",
            rationale=(
                f"retry limit reached at {ctx.provider_code} and no alternative "
                f"provider is eligible; "
                + ("compensating committed side effects before escalation"
                   if ctx.has_uncompensated_side_effects
                   else "escalating to a human operator")
            ),
            inputs=base_inputs,
        )


def _fallback_resume_state(ctx: FailureContext) -> TransactionState:
    """Where to restart after swapping providers.

    Swapping a supplier means redoing availability and reservation. Swapping a
    service centre means redoing authorization and booking. In both cases we go
    back to PROVIDER_SELECTED-1 so the selection step runs again - but never
    further back than that, so warranty and purchase verification are preserved.
    """
    if ctx.operation_type in {
        OperationType.CHECK_PART_AVAILABILITY,
        OperationType.RESERVE_PART,
    }:
        return TransactionState.COMPONENT_VALIDATED
    return TransactionState.COVERAGE_CHECKED


def describe_matrix() -> list[dict]:
    """Machine-readable failure/recovery matrix, surfaced by the admin API."""
    return [
        {"rule_id": "R1_UNKNOWN_OUTCOME_RECONCILE",
         "condition": "unknown outcome or timeout on a mutating operation",
         "strategy": "RECONCILE_VIA_IDEMPOTENCY",
         "example": "supplier reservation timed out after commit"},
        {"rule_id": "R2_PERMANENT_TERMINAL",
         "condition": "definitive business rejection for the transaction",
         "strategy": "TERMINATE (or COMPENSATE first)",
         "example": "warranty expired; serial not in OEM registry"},
        {"rule_id": "R3_COMPONENT_SCOPED_SUBSTITUTE",
         "condition": "component incompatible, alternatives exist",
         "strategy": "ALTERNATIVE_COMPONENT",
         "example": "requested battery rejected by compatibility engine"},
        {"rule_id": "R4_PROVIDER_SCOPED_FALLBACK",
         "condition": "provider-specific rejection, alternatives exist",
         "strategy": "FALLBACK_PROVIDER",
         "example": "supplier out of stock; service centre unauthorized"},
        {"rule_id": "R5_PERMANENT_UNCLASSIFIED",
         "condition": "permanent failure with no defined recovery",
         "strategy": "ESCALATE", "example": "unexpected 500 marked non-transient"},
        {"rule_id": "R6_TRANSIENT_RETRY",
         "condition": "transient failure, attempts remaining",
         "strategy": "RETRY_WITH_BACKOFF",
         "example": "marketplace timeout; warranty 503"},
        {"rule_id": "R7_RETRIES_EXHAUSTED_FALLBACK",
         "condition": "transient, attempts exhausted, alternatives exist",
         "strategy": "FALLBACK_PROVIDER",
         "example": "supplier repeatedly unavailable"},
        {"rule_id": "R8_EXHAUSTED_ESCALATE",
         "condition": "nothing left to try",
         "strategy": "ESCALATE (or COMPENSATE first)",
         "example": "all suppliers failing"},
    ]
