"""Request/response schemas for the ServiceMesh API.

Response models are explicit rather than returning ORM objects directly: the
transaction detail payload is assembled for the UI (timeline, operations,
decisions, failures, recovery actions) and the shape should be a deliberate
contract, not whatever the ORM happens to expose.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------- auth


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=6, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_minutes: int
    role: str
    user_id: str
    full_name: str
    customer_id: str | None = None
    provider_id: str | None = None
    provider_code: str | None = None


class UserOut(ORMModel):
    id: str
    email: str
    full_name: str
    role: str
    is_active: bool
    customer_id: str | None = None
    provider_id: str | None = None
    provider_code: str | None = None


# ---------------------------------------------------------------- customers


class CustomerOut(ORMModel):
    id: str
    external_ref: str
    full_name: str
    email: str
    phone: str | None = None
    region: str
    city: str | None = None


# ------------------------------------------------------------- transactions


class CreateTransactionRequest(BaseModel):
    order_ref: str = Field(..., min_length=3, max_length=64)
    serial_number: str = Field(..., min_length=3, max_length=64)
    issue_type: str = Field(..., min_length=2, max_length=64)
    issue_description: str = Field(..., min_length=5, max_length=2000)
    urgency: str = Field("NORMAL", pattern="^(LOW|NORMAL|HIGH|URGENT)$")
    requested_component_sku: str | None = Field(None, max_length=64)
    #: ADMIN/testing only: run the repair phase automatically instead of
    #: waiting for the service centre to report progress.
    auto_repair: bool = False
    #: Create only; do not start orchestration yet.
    defer_start: bool = False


class NaturalLanguageRequest(BaseModel):
    text: str = Field(..., min_length=10, max_length=4000)
    order_ref: str | None = Field(None, max_length=64)
    serial_number: str | None = Field(None, max_length=64)
    auto_repair: bool = False


class TransactionSummary(BaseModel):
    id: str
    reference: str
    correlation_id: str
    state: str
    state_label: str
    next_action: str
    resume_state: str
    progress_percent: float
    customer_id: str
    customer_name: str | None = None
    order_ref: str
    serial_number: str
    issue_type: str
    urgency: str
    outcome: str | None = None
    outcome_reason: str | None = None
    sla_due_at: datetime | None = None
    sla_breached: bool
    requires_manual_intervention: bool
    retry_total: int
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None


class StateHistoryOut(BaseModel):
    from_state: str | None
    to_state: str
    reason: str | None
    actor: str
    context: dict
    created_at: datetime


class AttemptOut(BaseModel):
    attempt_number: int
    status: str
    http_status: int | None
    latency_ms: float | None
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None


class OperationOut(BaseModel):
    id: str
    sequence: int
    operation_type: str
    status: str
    provider_code: str | None
    provider_name: str | None
    provider_kind: str | None
    attempt_count: int
    max_attempts: int
    latency_ms: float | None
    idempotency_key: str | None
    has_idempotency_protection: bool
    request_payload: dict
    response_payload: dict | None
    started_at: datetime | None
    completed_at: datetime | None
    attempts: list[AttemptOut]


class FailureOut(BaseModel):
    id: str
    failure_type: str
    failure_reason: str
    message: str | None
    state_at_failure: str
    attempt_number: int
    provider_code: str | None = None
    created_at: datetime


class RecoveryOut(BaseModel):
    id: str
    strategy: str
    rule_id: str
    rationale: str
    succeeded: bool | None
    resumed_from_state: str | None
    inputs: dict
    created_at: datetime


class DecisionOut(BaseModel):
    id: str
    decision_type: str
    verdict: str
    subject_label: str | None
    engine: str
    rule_version: str
    reasons: list
    inputs: dict
    scores: dict | None
    created_at: datetime


class EventOut(BaseModel):
    event_id: str
    event_type: str
    source_service: str
    payload: dict
    published: bool
    created_at: datetime


class ParticipantOut(BaseModel):
    provider_code: str
    provider_name: str
    kind: str
    role: str
    joined_state: str
    status: str
    operations_total: int
    operations_failed: int


class TimelineNode(BaseModel):
    """One node of the workflow visualisation on the detail page."""

    state: str
    label: str
    status: str  # COMPLETED | CURRENT | PENDING | FAILED | SKIPPED
    reached_at: datetime | None = None
    provider_code: str | None = None
    note: str | None = None


class TransactionDetail(TransactionSummary):
    issue_description: str
    raw_request_text: str | None = None
    nlp_extraction: dict | None = None
    purchase_evidence: dict | None = None
    product_evidence: dict | None = None
    warranty_evidence: dict | None = None
    coverage_evidence: dict | None = None
    model_code: str | None = None
    required_component_type: str | None = None
    selected_component_sku: str | None = None
    service_provider_code: str | None = None
    supplier_code: str | None = None
    part_reservation_ref: str | None = None
    service_booking_ref: str | None = None
    assigned_repair_person: str | None = None
    diagnosis_notes: str | None = None
    repair_notes: str | None = None
    verification_result: str | None = None
    part_delivery_status: str | None = None
    part_eta: str | None = None
    part_price: float | None = None
    supplier_decision: str | None = None
    notifications: list[dict] = []
    excluded_provider_codes: list[str] = []
    excluded_component_skus: list[str] = []
    timeline: list[TimelineNode] = []
    state_history: list[StateHistoryOut] = []
    operations: list[OperationOut] = []
    participants: list[ParticipantOut] = []
    failures: list[FailureOut] = []
    recovery_actions: list[RecoveryOut] = []
    decisions: list[DecisionOut] = []
    events: list[EventOut] = []
    audit_timeline: list[dict] = []


class DriveResponse(BaseModel):
    transaction_id: str
    reference: str
    final_state: str
    steps_executed: int
    recoveries_applied: int
    halted_reason: str
    message: str | None = None
    repair_status: str | None = None
    next_actions: list[str] = []


# ---------------------------------------------------------------- providers


class ProviderOut(ORMModel):
    id: str
    code: str
    name: str
    kind: str
    region: str
    is_active: bool
    sla_hours: int
    cost_index: float
    capacity_total: int
    capacity_used: int
    total_operations: int
    successful_operations: int
    failed_operations: int
    sla_violations: int


class ProviderStats(ProviderOut):
    success_rate: float
    avg_latency_ms: float
    available_capacity: int


class ProviderJobOut(BaseModel):
    """A job as seen by the organization that must act on it."""

    transaction_id: str
    reference: str
    transaction_state: str
    resume_state: str | None = None
    operation_id: str
    operation_type: str
    operation_status: str
    serial_number: str
    issue_type: str
    issue_description: str
    customer_name: str
    booking_ref: str | None = None
    reservation_ref: str | None = None
    component_sku: str | None = None
    assigned_repair_person: str | None = None
    repair_phase: str | None = None
    part_delivery_status: str | None = None
    part_eta: str | None = None
    created_at: datetime


class RepairUpdateRequest(BaseModel):
    status: str = Field(
        ..., pattern="^(CHECKED_IN|ACCEPTED|REJECTED|DIAGNOSING|AWAITING_PART|REPAIRING|COMPLETED|VERIFIED)$"
    )
    technician: str | None = Field(None, min_length=1, max_length=128)
    notes: str | None = Field(None, max_length=1000)


# -------------------------------------------------------------------- admin


class DashboardMetrics(BaseModel):
    total_transactions: int
    by_state: dict[str, int]
    completed: int
    failed: int
    rejected: int
    escalated: int
    in_progress: int
    recovering: int
    sla_breaches: int
    manual_interventions: int
    total_retries: int
    total_failures: int
    total_recovery_actions: int
    recovery_success_rate: float
    duplicate_operations_prevented: int
    avg_resolution_seconds: float | None
    avg_operations_per_transaction: float
    completion_rate: float
    generated_at: datetime


class SimulationRequest(BaseModel):
    service: str = Field(
        ...,
        pattern="^(marketplace|manufacturer|warranty|service_centre|parts_supplier)$",
    )
    mode: str
    operation: str | None = None
    count: int | None = Field(None, ge=1)
    latency_seconds: float = Field(0.0, ge=0)


class ErrorResponse(BaseModel):
    error: str
    message: str
    detail: dict | None = None


class SupplierDispatchRequest(BaseModel):
    eta: str | None = Field(None, max_length=64)


class AdminInterventionRequest(BaseModel):
    action: str = Field(..., pattern="^(RESUME|ESCALATE)$")
    reason: str = Field(..., min_length=3, max_length=500)
