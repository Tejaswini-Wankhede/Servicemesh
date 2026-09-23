"""ServiceMesh core ORM models.

Design note (important, and deliberate):
----------------------------------------
ServiceMesh does NOT own purchase records, warranty contracts or parts
inventory. Those belong to the marketplace, the warranty provider and the
supplier respectively, and each of those organizations keeps them in its own
database. Duplicating them here would quietly turn ServiceMesh into a data
warehouse and destroy the federation property the project is about.

What ServiceMesh stores instead is:
  * the Service Transaction itself (the thing no single organization owns),
  * a *reference* to the external record (order id, serial, claim id, ...),
  * the *evidence* returned by each organization at verification time,
  * every operation, attempt, failure, retry, recovery and decision.

Product/component/compatibility data is the one exception: ServiceMesh keeps a
local catalogue because the compatibility engine must be able to reason about
it deterministically and offline, and because compatibility spans OEM and
supplier boundaries (neither org owns the full graph).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from servicemesh.core.db import Base
from servicemesh.core.enums import (
    CompatibilityVerdict,
    FailureReason,
    FailureType,
    OperationStatus,
    OperationType,
    ProviderKind,
    RecoveryStrategy,
    TransactionState,
    UserRole,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_uuid() -> str:
    return str(uuid.uuid4())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


# ---------------------------------------------------------------------------
# Identity & access
# ---------------------------------------------------------------------------


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default=UserRole.CUSTOMER.value)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # A PROVIDER user is scoped to exactly one organization. This is enforced in
    # the API layer so a service centre cannot read the supplier's operations.
    provider_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("providers.id", ondelete="SET NULL"), nullable=True
    )
    customer_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )

    customer: Mapped[Customer | None] = relationship(back_populates="user")
    provider: Mapped[Provider | None] = relationship(back_populates="users")


class Customer(Base, TimestampMixin):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    external_ref: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    phone: Mapped[str | None] = mapped_column(String(32))
    region: Mapped[str] = mapped_column(String(8), nullable=False, default="IN", index=True)
    city: Mapped[str | None] = mapped_column(String(128))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)

    user: Mapped[User | None] = relationship(back_populates="customer")
    transactions: Mapped[list[ServiceTransaction]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# Product / component catalogue (compatibility domain)
# ---------------------------------------------------------------------------


class ProductModel(Base, TimestampMixin):
    """A model line, e.g. 'AX-14 Pro (2023)'. Owned conceptually by the OEM."""

    __tablename__ = "product_models"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    model_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    manufacturer_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="LAPTOP")
    release_year: Mapped[int | None] = mapped_column(Integer)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)

    components: Mapped[list[Component]] = relationship(back_populates="product_model")


class Component(Base, TimestampMixin):
    """A replaceable component/part SKU."""

    __tablename__ = "components"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    sku: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    component_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    manufacturer_code: Mapped[str] = mapped_column(String(64), nullable=False)
    # The model this part was originally designed for (may be null for generics)
    product_model_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("product_models.id", ondelete="SET NULL")
    )
    # Structured spec used by the deterministic compatibility rules
    specs: Mapped[dict] = mapped_column(JSON, default=dict)
    is_oem_certified: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    revision: Mapped[str] = mapped_column(String(16), default="A", nullable=False)

    product_model: Mapped[ProductModel | None] = relationship(back_populates="components")


class CompatibilityRule(Base, TimestampMixin):
    """Explicit, auditable compatibility edge: model <-> component.

    An explicit row is the authority. Absence of a row means UNKNOWN, not
    INCOMPATIBLE - an important distinction the compatibility engine relies on.
    """

    __tablename__ = "compatibility_rules"
    __table_args__ = (
        UniqueConstraint("product_model_id", "component_id", name="uq_compat_model_component"),
        Index("ix_compat_lookup", "product_model_id", "verdict"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    product_model_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_models.id", ondelete="CASCADE"), nullable=False
    )
    component_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("components.id", ondelete="CASCADE"), nullable=False
    )
    verdict: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CompatibilityVerdict.COMPATIBLE.value
    )
    # Additional constraints that must hold, e.g. {"min_bios": "1.4"}
    constraints: Mapped[dict] = mapped_column(JSON, default=dict)
    rule_source: Mapped[str] = mapped_column(String(64), default="OEM_MATRIX", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    # These relationships are not only for convenience: without them the
    # SQLAlchemy unit of work has no mapper-level dependency between
    # compatibility_rules and its two parent tables, and will happily emit the
    # child INSERT first, tripping the foreign key.
    product_model: Mapped[ProductModel] = relationship()
    component: Mapped[Component] = relationship()


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------


class Provider(Base, TimestampMixin):
    """A participating organization.

    Reliability figures here are ServiceMesh's own observations (updated from
    real operation outcomes), not numbers the provider self-reports.
    """

    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)
    adapter_key: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Selection inputs (hard constraints)
    region: Mapped[str] = mapped_column(String(8), default="IN", nullable=False, index=True)
    authorized_manufacturer_codes: Mapped[list] = mapped_column(JSON, default=list)
    capacity_total: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    capacity_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sla_hours: Mapped[int] = mapped_column(Integer, default=48, nullable=False)

    # Selection inputs (soft / scoring)
    cost_index: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    distance_km: Mapped[float] = mapped_column(Float, default=10.0, nullable=False)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)

    # Observed reliability (maintained by ServiceMesh)
    total_operations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    successful_operations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_operations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_latency_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    sla_violations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)

    users: Mapped[list[User]] = relationship(back_populates="provider")

    @property
    def success_rate(self) -> float:
        if self.total_operations == 0:
            return 0.85  # neutral prior for a provider we have never used
        return self.successful_operations / self.total_operations

    @property
    def avg_latency_ms(self) -> float:
        if self.total_operations == 0:
            return 500.0
        return self.total_latency_ms / self.total_operations

    @property
    def available_capacity(self) -> int:
        return max(0, self.capacity_total - self.capacity_used)


# ---------------------------------------------------------------------------
# The Service Transaction
# ---------------------------------------------------------------------------


class ServiceTransaction(Base, TimestampMixin):
    """A business-level distributed transaction spanning several organizations.

    This is NOT an ACID database transaction. No single commit spans the
    marketplace, the OEM, the warranty provider, the service centre and the
    supplier - they are separate systems with separate databases and separate
    owners. Consistency is therefore eventual, achieved through: a persisted
    state machine, idempotent operations, explicit compensation and, where
    automation is not safe, human escalation.
    """

    __tablename__ = "service_transactions"
    __table_args__ = (
        Index("ix_txn_state_created", "state", "created_at"),
        Index("ix_txn_customer_state", "customer_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    #: Human-facing reference, e.g. SM-2026-0001
    reference: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    #: Propagated into every log line and every outbound provider call
    correlation_id: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, default=new_uuid, index=True
    )

    customer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )

    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default=TransactionState.CREATED.value, index=True
    )
    #: Last state on the happy path that completed safely. Recovery resumes
    #: from here - this is what makes a failed transaction resumable rather
    #: than restartable.
    resume_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default=TransactionState.CREATED.value
    )
    previous_state: Mapped[str | None] = mapped_column(String(32))

    # --- customer request -------------------------------------------------
    order_ref: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    serial_number: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    issue_type: Mapped[str] = mapped_column(String(64), nullable=False)
    issue_description: Mapped[str] = mapped_column(Text, nullable=False)
    urgency: Mapped[str] = mapped_column(String(16), default="NORMAL", nullable=False)
    raw_request_text: Mapped[str | None] = mapped_column(Text)
    nlp_extraction: Mapped[dict | None] = mapped_column(JSON)

    # --- evidence gathered from each organization ------------------------
    purchase_evidence: Mapped[dict | None] = mapped_column(JSON)
    product_evidence: Mapped[dict | None] = mapped_column(JSON)
    warranty_evidence: Mapped[dict | None] = mapped_column(JSON)
    coverage_evidence: Mapped[dict | None] = mapped_column(JSON)

    # --- decisions --------------------------------------------------------
    product_model_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("product_models.id", ondelete="SET NULL")
    )
    required_component_type: Mapped[str | None] = mapped_column(String(64))
    selected_component_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("components.id", ondelete="SET NULL")
    )
    selected_service_provider_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("providers.id", ondelete="SET NULL")
    )
    selected_supplier_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("providers.id", ondelete="SET NULL")
    )
    #: Providers already tried and ruled out (fallback logic reads this)
    excluded_provider_ids: Mapped[list] = mapped_column(JSON, default=list)
    excluded_component_ids: Mapped[list] = mapped_column(JSON, default=list)

    # --- external handles (needed for compensation) ----------------------
    part_reservation_ref: Mapped[str | None] = mapped_column(String(64))
    service_booking_ref: Mapped[str | None] = mapped_column(String(64))
    # Operational facts reported by the service/supplier portals. These are
    # explicit columns because they are customer-visible business state, not
    # an opaque workflow blob.
    assigned_repair_person: Mapped[str | None] = mapped_column(String(128))
    diagnosis_notes: Mapped[str | None] = mapped_column(Text)
    repair_notes: Mapped[str | None] = mapped_column(Text)
    verification_result: Mapped[str | None] = mapped_column(String(32))
    part_delivery_status: Mapped[str | None] = mapped_column(String(32))
    part_eta: Mapped[str | None] = mapped_column(String(64))
    part_price: Mapped[float | None] = mapped_column(Float)
    supplier_decision: Mapped[str | None] = mapped_column(String(32))

    # --- SLA / outcome ----------------------------------------------------
    sla_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sla_breached: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(32))
    outcome_reason: Mapped[str | None] = mapped_column(Text)
    requires_manual_intervention: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    manual_interventions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retry_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    customer: Mapped[Customer] = relationship(back_populates="transactions")
    state_history: Mapped[list[TransactionStateHistory]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan",
        order_by="TransactionStateHistory.created_at",
    )
    operations: Mapped[list[ProviderOperation]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan",
        order_by="ProviderOperation.sequence",
    )
    participants: Mapped[list[TransactionParticipant]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan"
    )
    events: Mapped[list[EventRecord]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan",
        order_by="EventRecord.created_at",
    )
    decisions: Mapped[list[DecisionRecord]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan",
        order_by="DecisionRecord.created_at",
    )
    recovery_actions: Mapped[list[RecoveryAction]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan",
        order_by="RecoveryAction.created_at",
    )


class TransactionStateHistory(Base):
    """Append-only record of every state change. Never updated, never deleted."""

    __tablename__ = "transaction_state_history"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    transaction_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("service_transactions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(64), default="ORCHESTRATOR", nullable=False)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    transaction: Mapped[ServiceTransaction] = relationship(back_populates="state_history")


class TransactionParticipant(Base, TimestampMixin):
    """Which organizations are involved in this transaction, and in what role."""

    __tablename__ = "transaction_participants"
    __table_args__ = (
        UniqueConstraint("transaction_id", "provider_id", name="uq_participant"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    transaction_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("service_transactions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    provider_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("providers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    joined_state: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", nullable=False)

    transaction: Mapped[ServiceTransaction] = relationship(back_populates="participants")
    provider: Mapped[Provider] = relationship()


class ProviderOperation(Base, TimestampMixin):
    """One logical call to one organization.

    A ProviderOperation may have many OperationAttempts (retries). The
    idempotency_key is stable across attempts - that is precisely what makes a
    retry safe.
    """

    __tablename__ = "provider_operations"
    __table_args__ = (
        Index("ix_op_txn_seq", "transaction_id", "sequence"),
        Index("ix_op_provider_status", "provider_id", "status"),
        UniqueConstraint("idempotency_key", name="uq_operation_idempotency_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    transaction_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("service_transactions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    provider_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("providers.id", ondelete="SET NULL"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    operation_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=OperationStatus.PENDING.value, index=True
    )
    #: Stable across retries. NULL for read-only operations.
    idempotency_key: Mapped[str | None] = mapped_column(String(80), unique=True)

    request_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    response_payload: Mapped[dict | None] = mapped_column(JSON)

    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    latency_ms: Mapped[float | None] = mapped_column(Float)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    is_compensated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    compensated_by_operation_id: Mapped[str | None] = mapped_column(String(36))

    transaction: Mapped[ServiceTransaction] = relationship(back_populates="operations")
    provider: Mapped[Provider | None] = relationship()
    attempts: Mapped[list[OperationAttempt]] = relationship(
        back_populates="operation", cascade="all, delete-orphan",
        order_by="OperationAttempt.attempt_number",
    )
    failures: Mapped[list[FailureRecord]] = relationship(
        back_populates="operation", cascade="all, delete-orphan"
    )


class OperationAttempt(Base):
    """One physical invocation. Retries create additional rows, never overwrite."""

    __tablename__ = "operation_attempts"
    __table_args__ = (
        UniqueConstraint("operation_id", "attempt_number", name="uq_attempt_number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    operation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("provider_operations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    request_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    response_snapshot: Mapped[dict | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    operation: Mapped[ProviderOperation] = relationship(back_populates="attempts")


class FailureRecord(Base):
    __tablename__ = "failure_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    transaction_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("service_transactions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    operation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("provider_operations.id", ondelete="CASCADE"), index=True
    )
    provider_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("providers.id", ondelete="SET NULL")
    )
    failure_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    failure_reason: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    message: Mapped[str | None] = mapped_column(Text)
    state_at_failure: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    operation: Mapped[ProviderOperation | None] = relationship(back_populates="failures")


class RecoveryAction(Base):
    """What the recovery engine decided, and why. Fully explainable."""

    __tablename__ = "recovery_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    transaction_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("service_transactions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    failure_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("failure_records.id", ondelete="SET NULL")
    )
    operation_id: Mapped[str | None] = mapped_column(String(36))
    strategy: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)
    inputs: Mapped[dict] = mapped_column(JSON, default=dict)
    succeeded: Mapped[bool | None] = mapped_column(Boolean)
    resumed_from_state: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    transaction: Mapped[ServiceTransaction] = relationship(back_populates="recovery_actions")


class DecisionRecord(Base):
    """Policy / selection / compatibility decisions, stored for explainability.

    One table rather than three because they share the same shape (inputs,
    verdict, reasons, rule version) and are always read together on the
    transaction detail page.
    """

    __tablename__ = "decision_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    transaction_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("service_transactions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    decision_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str | None] = mapped_column(String(36))
    subject_label: Mapped[str | None] = mapped_column(String(255))
    engine: Mapped[str] = mapped_column(String(48), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(16), default="v1", nullable=False)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    inputs: Mapped[dict] = mapped_column(JSON, default=dict)
    scores: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    transaction: Mapped[ServiceTransaction] = relationship(back_populates="decisions")


class EventRecord(Base):
    """Persisted domain event.

    Persisted first, published second (transactional-outbox style). If the
    broker is down the event is not lost, and the audit trail does not depend
    on Kafka being available.
    """

    __tablename__ = "event_records"
    __table_args__ = (
        Index("ix_event_txn_type", "transaction_id", "event_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    event_id: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, default=new_uuid
    )
    transaction_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("service_transactions.id", ondelete="CASCADE"), index=True
    )
    correlation_id: Mapped[str | None] = mapped_column(String(36), index=True)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    source_service: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    consumed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )

    transaction: Mapped[ServiceTransaction | None] = relationship(back_populates="events")


class ProcessedEvent(Base):
    """Consumer-side dedupe table: makes event consumers idempotent."""

    __tablename__ = "processed_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    consumer: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (UniqueConstraint("event_id", "consumer", name="uq_processed_event"),)


class AuditEvent(Base):
    """Security/operational audit distinct from domain events."""

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    transaction_id: Mapped[str | None] = mapped_column(String(36), index=True)
    actor_id: Mapped[str | None] = mapped_column(String(36))
    actor_role: Mapped[str | None] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target: Mapped[str | None] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    transaction_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("service_transactions.id", ondelete="CASCADE"), index=True
    )
    recipient_type: Mapped[str] = mapped_column(String(24), nullable=False)
    recipient_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(24), default="IN_APP", nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ProviderMetricSnapshot(Base):
    """Periodic rollup of provider behaviour - the ML training feature source."""

    __tablename__ = "provider_metric_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    provider_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("providers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    operations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    successes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sla_violations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    avg_latency_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


__all__ = [
    "Base", "User", "Customer", "ProductModel", "Component", "CompatibilityRule",
    "Provider", "ServiceTransaction", "TransactionStateHistory", "TransactionParticipant",
    "ProviderOperation", "OperationAttempt", "FailureRecord", "RecoveryAction",
    "DecisionRecord", "EventRecord", "ProcessedEvent", "AuditEvent", "Notification",
    "ProviderMetricSnapshot", "utcnow", "new_uuid",
    # re-exported for convenience in modules that import from models
    "TransactionState", "OperationType", "OperationStatus", "FailureType",
    "FailureReason", "RecoveryStrategy", "ProviderKind", "CompatibilityVerdict", "UserRole",
]
