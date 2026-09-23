"""Domain vocabulary for ServiceMesh.

These enums are the contract between the orchestrator, the persistence layer,
the API and the frontend. They are stored as strings in the database so that a
human reading a row (or an auditor reading an export) can understand it without
a lookup table.
"""

from __future__ import annotations

from enum import StrEnum


class TransactionState(StrEnum):
    """Lifecycle of a Service Transaction.

    The happy path is strictly ordered; the failure/control states can be
    entered from (almost) anywhere and are exited back onto the happy path.
    """

    # --- happy path ---
    CREATED = "CREATED"
    PURCHASE_VERIFIED = "PURCHASE_VERIFIED"
    PRODUCT_VERIFIED = "PRODUCT_VERIFIED"
    WARRANTY_VERIFIED = "WARRANTY_VERIFIED"
    COVERAGE_CHECKED = "COVERAGE_CHECKED"
    PROVIDER_SELECTED = "PROVIDER_SELECTED"
    COMPONENT_VALIDATED = "COMPONENT_VALIDATED"
    PART_REQUESTED = "PART_REQUESTED"
    PART_CONFIRMED = "PART_CONFIRMED"
    REPAIR_SCHEDULED = "REPAIR_SCHEDULED"
    REPAIR_IN_PROGRESS = "REPAIR_IN_PROGRESS"
    REPAIR_COMPLETED = "REPAIR_COMPLETED"
    SERVICE_VERIFIED = "SERVICE_VERIFIED"
    CLOSED = "CLOSED"

    # --- failure / control ---
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    RETRYING = "RETRYING"
    WAITING = "WAITING"
    ESCALATED = "ESCALATED"
    COMPENSATING = "COMPENSATING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


#: States from which no further orchestration will occur.
TERMINAL_STATES: frozenset[TransactionState] = frozenset(
    {
        TransactionState.CLOSED,
        TransactionState.CANCELLED,
        TransactionState.REJECTED,
        TransactionState.FAILED,
    }
)

#: States that mean "a human must look at this before anything else happens".
HUMAN_ATTENTION_STATES: frozenset[TransactionState] = frozenset(
    {TransactionState.ESCALATED}
)

#: The ordered happy path. The orchestrator uses this to know what comes next
#: and, critically, to know what has *already* been done after a crash.
HAPPY_PATH: tuple[TransactionState, ...] = (
    TransactionState.CREATED,
    TransactionState.PURCHASE_VERIFIED,
    TransactionState.PRODUCT_VERIFIED,
    TransactionState.WARRANTY_VERIFIED,
    TransactionState.COVERAGE_CHECKED,
    TransactionState.PROVIDER_SELECTED,
    TransactionState.COMPONENT_VALIDATED,
    TransactionState.PART_REQUESTED,
    TransactionState.PART_CONFIRMED,
    TransactionState.REPAIR_SCHEDULED,
    TransactionState.REPAIR_IN_PROGRESS,
    TransactionState.REPAIR_COMPLETED,
    TransactionState.SERVICE_VERIFIED,
    TransactionState.CLOSED,
)


class OperationType(StrEnum):
    """The standardized internal operation vocabulary.

    Provider adapters translate each of these into whatever the external
    organization's API actually looks like. Adding a new organization means
    writing an adapter, not changing the orchestrator.
    """

    VERIFY_PURCHASE = "VERIFY_PURCHASE"
    VERIFY_PRODUCT = "VERIFY_PRODUCT"
    VERIFY_WARRANTY = "VERIFY_WARRANTY"
    CHECK_COVERAGE = "CHECK_COVERAGE"
    CHECK_PROVIDER_AUTHORIZATION = "CHECK_PROVIDER_AUTHORIZATION"
    CHECK_COMPATIBILITY = "CHECK_COMPATIBILITY"
    CHECK_PART_AVAILABILITY = "CHECK_PART_AVAILABILITY"
    RESERVE_PART = "RESERVE_PART"
    RELEASE_PART = "RELEASE_PART"  # compensating action for RESERVE_PART
    BOOK_SERVICE = "BOOK_SERVICE"
    CANCEL_SERVICE = "CANCEL_SERVICE"  # compensating action for BOOK_SERVICE
    UPDATE_REPAIR = "UPDATE_REPAIR"
    CONFIRM_COMPLETION = "CONFIRM_COMPLETION"
    GET_OPERATION_STATUS = "GET_OPERATION_STATUS"
    DISPATCH_PART = "DISPATCH_PART"


#: Operations that change state at the external organization and therefore
#: require an idempotency key. Read-only checks do not.
MUTATING_OPERATIONS: frozenset[OperationType] = frozenset(
    {
        OperationType.RESERVE_PART,
        OperationType.RELEASE_PART,
        OperationType.BOOK_SERVICE,
        OperationType.CANCEL_SERVICE,
        OperationType.UPDATE_REPAIR,
        OperationType.CONFIRM_COMPLETION,
        OperationType.DISPATCH_PART,
    }
)

#: Maps a mutating operation to the operation that undoes it.
COMPENSATION_MAP: dict[OperationType, OperationType] = {
    OperationType.RESERVE_PART: OperationType.RELEASE_PART,
    OperationType.BOOK_SERVICE: OperationType.CANCEL_SERVICE,
}


class OperationStatus(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"  # provider said "no" and meant it (business rejection)
    TIMED_OUT = "TIMED_OUT"
    UNKNOWN = "UNKNOWN"  # we do not know whether the side effect happened
    COMPENSATED = "COMPENSATED"


class FailureType(StrEnum):
    """Classification drives recovery. This is the most important enum here.

    TRANSIENT  -> the operation may succeed if repeated (retry is safe+useful)
    PERMANENT  -> repeating will produce the same answer (do NOT retry)
    UNKNOWN    -> the side effect may or may not have happened (must reconcile
                  via idempotency key before doing anything else)
    """

    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"
    UNKNOWN = "UNKNOWN"


class FailureReason(StrEnum):
    TIMEOUT = "TIMEOUT"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    BUSINESS_REJECTION = "BUSINESS_REJECTION"
    NOT_FOUND = "NOT_FOUND"
    UNAUTHORIZED = "UNAUTHORIZED"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    INCOMPATIBLE_COMPONENT = "INCOMPATIBLE_COMPONENT"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    NO_ELIGIBLE_PROVIDER = "NO_ELIGIBLE_PROVIDER"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class RecoveryStrategy(StrEnum):
    RETRY = "RETRY"
    RETRY_WITH_BACKOFF = "RETRY_WITH_BACKOFF"
    RECONCILE_VIA_IDEMPOTENCY = "RECONCILE_VIA_IDEMPOTENCY"
    FALLBACK_PROVIDER = "FALLBACK_PROVIDER"
    ALTERNATIVE_COMPONENT = "ALTERNATIVE_COMPONENT"
    COMPENSATE = "COMPENSATE"
    ESCALATE = "ESCALATE"
    TERMINATE = "TERMINATE"
    WAIT = "WAIT"


class ProviderKind(StrEnum):
    MARKETPLACE = "MARKETPLACE"
    MANUFACTURER = "MANUFACTURER"
    WARRANTY = "WARRANTY"
    SERVICE_CENTRE = "SERVICE_CENTRE"
    PARTS_SUPPLIER = "PARTS_SUPPLIER"


class FailureMode(StrEnum):
    """Modes the failure-simulation framework can push a provider into."""

    NORMAL = "NORMAL"
    TIMEOUT = "TIMEOUT"
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    HIGH_LATENCY = "HIGH_LATENCY"


class UserRole(StrEnum):
    CUSTOMER = "CUSTOMER"
    PROVIDER = "PROVIDER"
    REPAIR_PERSON = "REPAIR_PERSON"
    ADMIN = "ADMIN"


class CompatibilityVerdict(StrEnum):
    COMPATIBLE = "COMPATIBLE"
    INCOMPATIBLE = "INCOMPATIBLE"
    UNKNOWN = "UNKNOWN"


class EventType(StrEnum):
    TRANSACTION_CREATED = "TransactionCreated"
    PURCHASE_VERIFIED = "PurchaseVerified"
    PRODUCT_VERIFIED = "ProductVerified"
    WARRANTY_VERIFIED = "WarrantyVerified"
    COVERAGE_CHECKED = "CoverageChecked"
    PROVIDER_SELECTED = "ProviderSelected"
    COMPONENT_VALIDATED = "ComponentValidated"
    PART_REQUESTED = "PartRequested"
    PART_RESERVED = "PartReserved"
    SERVICE_SCHEDULED = "ServiceScheduled"
    REPAIR_STARTED = "RepairStarted"
    REPAIR_COMPLETED = "RepairCompleted"
    OPERATION_FAILED = "OperationFailed"
    RETRY_STARTED = "RetryStarted"
    RECOVERY_STARTED = "RecoveryStarted"
    COMPENSATION_EXECUTED = "CompensationExecuted"
    TRANSACTION_ESCALATED = "TransactionEscalated"
    TRANSACTION_REJECTED = "TransactionRejected"
    TRANSACTION_CLOSED = "TransactionClosed"
    STATE_CHANGED = "StateChanged"
