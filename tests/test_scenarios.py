"""End-to-end Service Transaction scenarios.

These run the real orchestrator against the real provider services. Nothing is
mocked: assertions are made against persisted state and against the
organizations' own databases.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from servicemesh.core.enums import (
    FailureType,
    OperationStatus,
    OperationType,
    RecoveryStrategy,
    TransactionState,
)
from servicemesh.core.models import (
    DecisionRecord,
    FailureRecord,
    ProviderOperation,
    RecoveryAction,
)
from servicemesh.orchestration.service import (
    create_transaction,
    resume_transaction,
    run_transaction,
)
from tests.conftest import make_request

pytestmark = pytest.mark.e2e


async def _run(db, customer, *, auto_repair=True, **overrides):
    txn = create_transaction(db, customer=customer, **make_request(**overrides))
    result = await run_transaction(db, txn, auto_repair=auto_repair)
    db.flush()
    return txn, result


def _ops(db, txn, op_type: OperationType):
    return list(db.scalars(
        select(ProviderOperation).where(
            ProviderOperation.transaction_id == txn.id,
            ProviderOperation.operation_type == op_type.value,
        )
    ))


def _recoveries(db, txn):
    return list(db.scalars(
        select(RecoveryAction).where(RecoveryAction.transaction_id == txn.id)
    ))


# ===========================================================================
# Scenario 1: normal transaction
# ===========================================================================


@pytest.mark.asyncio
async def test_scenario_1_normal_transaction_closes(db, customer):
    txn, result = await _run(db, customer)

    assert result.final_state is TransactionState.CLOSED
    assert txn.outcome == "RESOLVED"
    assert txn.part_reservation_ref is not None
    assert txn.service_booking_ref is not None

    # All five organizations participated.
    kinds = {p.provider.kind for p in txn.participants}
    assert kinds == {
        "MARKETPLACE", "MANUFACTURER", "WARRANTY", "PARTS_SUPPLIER", "SERVICE_CENTRE"
    }

    # The full happy path was walked, in order.
    visited = [h.to_state for h in txn.state_history]
    for expected in (
        "CREATED", "PURCHASE_VERIFIED", "PRODUCT_VERIFIED", "WARRANTY_VERIFIED",
        "COVERAGE_CHECKED", "PROVIDER_SELECTED", "COMPONENT_VALIDATED",
        "PART_REQUESTED", "PART_CONFIRMED", "REPAIR_SCHEDULED",
        "REPAIR_COMPLETED", "SERVICE_VERIFIED", "CLOSED",
    ):
        assert expected in visited, f"{expected} missing from {visited}"

    # Every operation succeeded and each is individually auditable.
    ops = list(txn.operations)
    assert ops, "no provider operations recorded"
    assert all(o.status == OperationStatus.SUCCEEDED.value for o in ops)
    assert all(o.attempts for o in ops)


@pytest.mark.asyncio
async def test_normal_transaction_reserves_stock_exactly_once(db, customer):
    from providers.parts_supplier.app import SupplierReservation, store

    txn, _ = await _run(db, customer)

    with store.session() as sup_db:
        rows = list(sup_db.scalars(
            select(SupplierReservation).where(
                SupplierReservation.external_ref == txn.reference
            )
        ))
    assert len(rows) == 1, f"expected exactly one reservation, got {len(rows)}"


# ===========================================================================
# Scenario 2: warranty rejection
# ===========================================================================


@pytest.mark.asyncio
async def test_scenario_2_expired_warranty_terminates_cleanly(db, customer):
    txn, result = await _run(
        db, customer, order_ref="ORD-100002", serial_number="SN-AX14-0002"
    )

    assert result.final_state is TransactionState.REJECTED
    assert txn.outcome == "REJECTED"
    assert "expired" in txn.outcome_reason.lower()

    # Earlier successful work is still recorded - the audit trail is intact.
    assert txn.purchase_evidence["verified"] is True
    assert txn.product_evidence["verified"] is True
    assert txn.warranty_evidence["valid"] is False

    # Critically: a permanent business rejection must NOT have been retried.
    warranty_ops = _ops(db, txn, OperationType.VERIFY_WARRANTY)
    assert len(warranty_ops) == 1
    assert warranty_ops[0].attempt_count == 1
    assert not _recoveries(db, txn), "a terminal rejection must not trigger recovery"

    # No downstream side effects were created.
    assert txn.part_reservation_ref is None
    assert txn.service_booking_ref is None


@pytest.mark.asyncio
async def test_voided_warranty_terminates(db):
    from servicemesh.core.models import Customer

    meera = db.scalar(select(Customer).where(Customer.external_ref == "CUST-0003"))
    txn, result = await _run(
        db, meera, order_ref="ORD-100004", serial_number="SN-AX14-0004"
    )
    assert result.final_state is TransactionState.REJECTED
    # The warranty provider voided the contract; its own reason is carried
    # through verbatim so the customer explanation is the organization's, not
    # ServiceMesh's paraphrase.
    assert txn.warranty_evidence["valid"] is False
    assert "third-party repair" in txn.outcome_reason.lower()


@pytest.mark.asyncio
async def test_coverage_exclusion_rejects_after_warranty_verified(db):
    """Contract is valid, but this plan excludes battery failure."""
    from servicemesh.core.models import Customer

    meera = db.scalar(select(Customer).where(Customer.external_ref == "CUST-0003"))
    txn, result = await _run(
        db, meera, order_ref="ORD-100003", serial_number="SN-AX14-0003"
    )

    assert result.final_state is TransactionState.REJECTED
    assert txn.warranty_evidence["valid"] is True      # contract is fine
    assert txn.coverage_evidence["covered"] is False   # this repair is not
    visited = [h.to_state for h in txn.state_history]
    assert "WARRANTY_VERIFIED" in visited
    assert "COVERAGE_CHECKED" not in visited


# ===========================================================================
# Scenario 3: unauthorized provider -> fallback
# ===========================================================================


@pytest.mark.asyncio
async def test_scenario_3_unauthorized_provider_is_rejected_and_replaced(
    db, nagpur_customer
):
    """The nearest/cheapest centre for this customer is not OEM-authorized."""
    txn, result = await _run(
        db, nagpur_customer, order_ref="ORD-100005", serial_number="SN-AX15-0001"
    )

    decisions = list(db.scalars(
        select(DecisionRecord).where(
            DecisionRecord.transaction_id == txn.id,
            DecisionRecord.decision_type == "PROVIDER_SELECTION",
        )
    ))
    rejected = [d for d in decisions if d.verdict == "REJECTED"]
    selected = [d for d in decisions if d.verdict == "SELECTED"]

    assert rejected, "expected the unauthorized provider to be rejected"
    assert rejected[0].subject_label == "SVC-NAG-GREY"
    assert any("OEM" in r or "authoriz" in r.lower()
               for r in rejected[0].reasons)

    assert selected, "expected a different, authorized provider to be selected"
    assert selected[0].subject_label != "SVC-NAG-GREY"

    # The transaction continued rather than failing.
    assert result.final_state is TransactionState.CLOSED


# ===========================================================================
# Scenario 4: incompatible component
# ===========================================================================


@pytest.mark.asyncio
async def test_scenario_4_incompatible_component_is_rejected_and_substituted(
    db, customer
):
    """Customer nominates a non-certified generic battery."""
    txn = create_transaction(
        db, customer=customer,
        **make_request(),
        nlp_extraction={"requested_component_sku": "BAT-GENERIC-X"},
    )
    result = await run_transaction(db, txn, auto_repair=True)
    db.flush()

    compat = list(db.scalars(
        select(DecisionRecord).where(
            DecisionRecord.transaction_id == txn.id,
            DecisionRecord.decision_type == "COMPATIBILITY",
        )
    ))
    verdicts = {(d.subject_label, d.verdict) for d in compat}
    assert ("BAT-GENERIC-X", "INCOMPATIBLE") in verdicts

    # A different, compatible part was chosen and the transaction continued.
    chosen = [d for d in compat if d.verdict == "COMPATIBLE"]
    assert chosen, "expected a compatible substitute to be selected"
    assert chosen[0].subject_label != "BAT-GENERIC-X"
    assert result.final_state is TransactionState.CLOSED


@pytest.mark.asyncio
async def test_unknown_compatibility_is_not_treated_as_compatible(db):
    """A component with no published rule must never be auto-selected."""
    from servicemesh.core.models import Component, ProductModel
    from servicemesh.engines.compatibility import CompatibilityEngine

    engine = CompatibilityEngine(db)
    zn13 = db.scalar(select(ProductModel).where(
        ProductModel.model_code == "ZN13-ULTRA-2024"))
    fan = db.scalar(select(Component).where(Component.sku == "FAN-AX14-A"))

    check = engine.check(zn13.id, fan.id, {})
    assert check.verdict.value == "UNKNOWN"
    assert not check.is_compatible


# ===========================================================================
# Scenario 5: supplier timeout -> retry -> resume without repeating work
# ===========================================================================


@pytest.mark.asyncio
async def test_scenario_5_supplier_timeout_retries_and_resumes(
    db, customer, simulators
):
    """Fail the availability check twice, then let it succeed."""
    simulators["parts_supplier"].set_mode(
        "TEMPORARILY_UNAVAILABLE", operation="inventory.check", count=2
    )

    txn, result = await _run(db, customer)

    assert result.final_state is TransactionState.CLOSED, txn.outcome_reason

    failures = list(db.scalars(
        select(FailureRecord).where(FailureRecord.transaction_id == txn.id)
    ))
    assert failures, "expected the outage to be recorded"
    assert all(f.failure_type == FailureType.TRANSIENT.value for f in failures)

    recoveries = _recoveries(db, txn)
    assert recoveries
    assert any(r.strategy == RecoveryStrategy.RETRY_WITH_BACKOFF.value
               for r in recoveries)

    # The decisive assertion: upstream organizations were NOT re-contacted.
    assert len(_ops(db, txn, OperationType.VERIFY_PURCHASE)) == 1
    assert len(_ops(db, txn, OperationType.VERIFY_PRODUCT)) == 1
    assert len(_ops(db, txn, OperationType.VERIFY_WARRANTY)) == 1
    assert _ops(db, txn, OperationType.VERIFY_PURCHASE)[0].attempt_count == 1

    # RETRYING was entered and then left.
    visited = [h.to_state for h in txn.state_history]
    assert "RETRYING" in visited
    assert visited[-1] == "CLOSED"


@pytest.mark.asyncio
async def test_transient_failure_records_every_attempt(db, customer, simulators):
    simulators["parts_supplier"].set_mode(
        "TEMPORARILY_UNAVAILABLE", operation="inventory.check", count=2
    )
    txn, _ = await _run(db, customer)

    avail_ops = _ops(db, txn, OperationType.CHECK_PART_AVAILABILITY)
    total_attempts = sum(o.attempt_count for o in avail_ops)
    assert total_attempts >= 3, f"expected retries, saw {total_attempts} attempts"
    for op in avail_ops:
        assert len(op.attempts) == op.attempt_count


# ===========================================================================
# Scenario 6: persistent failure -> escalation
# ===========================================================================


@pytest.mark.asyncio
async def test_scenario_6_persistent_failure_escalates(db, customer, simulators):
    """Every supplier is down. Retries exhaust, no fallback works, escalate."""
    simulators["parts_supplier"].set_mode("TEMPORARILY_UNAVAILABLE")

    txn, result = await _run(db, customer)

    assert result.final_state is TransactionState.ESCALATED
    assert txn.requires_manual_intervention is True
    assert txn.manual_interventions >= 1

    recoveries = _recoveries(db, txn)
    strategies = {r.strategy for r in recoveries}
    assert RecoveryStrategy.RETRY_WITH_BACKOFF.value in strategies
    assert RecoveryStrategy.ESCALATE.value in strategies

    # Escalation must still be recoverable and fully auditable.
    assert txn.resume_state in {
        TransactionState.COMPONENT_VALIDATED.value,
        TransactionState.COVERAGE_CHECKED.value,
        TransactionState.PROVIDER_SELECTED.value,
    }
    assert list(db.scalars(
        select(FailureRecord).where(FailureRecord.transaction_id == txn.id)
    ))


@pytest.mark.asyncio
async def test_escalated_transaction_resumes_after_outage_clears(
    db, customer, simulators
):
    """The recovery claim that matters: resume, do not restart."""
    simulators["parts_supplier"].set_mode("TEMPORARILY_UNAVAILABLE")
    txn, result = await _run(db, customer)
    assert result.final_state is TransactionState.ESCALATED

    purchase_ops_before = len(_ops(db, txn, OperationType.VERIFY_PURCHASE))
    warranty_ops_before = len(_ops(db, txn, OperationType.VERIFY_WARRANTY))

    simulators["parts_supplier"].reset()
    result2 = await resume_transaction(db, txn, auto_repair=True)
    db.flush()

    assert result2.final_state is TransactionState.CLOSED
    # Upstream organizations were not contacted again during the resume.
    assert len(_ops(db, txn, OperationType.VERIFY_PURCHASE)) == purchase_ops_before
    assert len(_ops(db, txn, OperationType.VERIFY_WARRANTY)) == warranty_ops_before


# ===========================================================================
# Scenario 7: duplicate operation / idempotency
# ===========================================================================


@pytest.mark.asyncio
async def test_scenario_7_repeated_reservation_does_not_double_book(db, customer):
    """Drive the reserve step repeatedly with the same business intent."""
    from providers.parts_supplier.app import SupplierReservation, SupplierStock
    from providers.parts_supplier.app import store as sup_store
    from servicemesh.core.models import Component, Provider
    from servicemesh.orchestration.workflow import Workflow

    txn = create_transaction(db, customer=customer, **make_request())
    await run_transaction(db, txn, auto_repair=False)
    db.flush()
    assert txn.part_reservation_ref is not None

    with sup_store.session() as s:
        stock_before = s.scalar(
            select(SupplierStock).where(
                SupplierStock.sku == "BAT-AX14-B", SupplierStock.supplier_code == "SUP-A"
            )
        ).reserved

    # Issue the identical reservation three more times.
    wf = Workflow(db)
    supplier = db.get(Provider, txn.selected_supplier_id)
    component = db.get(Component, txn.selected_component_id)
    for _ in range(3):
        _, res = await wf.orc.execute_operation(
            txn, OperationType.RESERVE_PART, supplier,
            {"sku": component.sku, "quantity": 1, "external_ref": txn.reference},
        )
        assert res.success
    db.flush()

    with sup_store.session() as s:
        stock_after = s.scalar(
            select(SupplierStock).where(
                SupplierStock.sku == "BAT-AX14-B", SupplierStock.supplier_code == "SUP-A"
            )
        ).reserved
        reservations = list(s.scalars(
            select(SupplierReservation).where(
                SupplierReservation.external_ref == txn.reference
            )
        ))

    assert stock_after == stock_before, "duplicate requests consumed extra stock"
    assert len(reservations) == 1, "duplicate reservation rows were created"

    # ServiceMesh recorded one logical operation, not four.
    reserve_ops = _ops(db, txn, OperationType.RESERVE_PART)
    assert len(reserve_ops) == 1
    assert reserve_ops[0].idempotency_key is not None


@pytest.mark.asyncio
async def test_idempotency_key_is_stable_across_attempts(db, customer):
    from servicemesh.orchestration.idempotency import make_idempotency_key

    payload = {"sku": "BAT-AX14-A", "quantity": 1}
    k1 = make_idempotency_key("txn-1", OperationType.RESERVE_PART, "prov-1", payload)
    k2 = make_idempotency_key("txn-1", OperationType.RESERVE_PART, "prov-1", dict(payload))
    assert k1 == k2

    # Different part on the same transaction must NOT collide.
    k3 = make_idempotency_key(
        "txn-1", OperationType.RESERVE_PART, "prov-1", {"sku": "SCR-AX14-A", "quantity": 1}
    )
    assert k1 != k3


# ===========================================================================
# Scenario: unknown outcome -> reconciliation
# ===========================================================================


@pytest.mark.asyncio
async def test_unknown_outcome_is_reconciled_not_guessed(db, customer, simulators):
    """Reservation commits at the supplier, but the response never arrives."""
    from providers.parts_supplier.app import SupplierReservation
    from providers.parts_supplier.app import store as sup_store

    simulators["parts_supplier"].set_mode(
        "TIMEOUT_AFTER_COMMIT", operation="reservations.create", count=1
    )

    txn, result = await _run(db, customer)

    recoveries = _recoveries(db, txn)
    assert any(
        r.strategy == RecoveryStrategy.RECONCILE_VIA_IDEMPOTENCY.value
        for r in recoveries
    ), f"expected reconciliation, saw {[r.strategy for r in recoveries]}"

    # Exactly one reservation exists at the supplier - not zero, not two.
    with sup_store.session() as s:
        refs = [
            r.reservation_ref for r in s.scalars(
                select(SupplierReservation).where(
                    SupplierReservation.external_ref == txn.reference
                )
            )
        ]
    assert len(refs) == 1
    assert txn.part_reservation_ref == refs[0]
    assert result.final_state is TransactionState.CLOSED


# ===========================================================================
# Scenario: out of stock -> fallback supplier
# ===========================================================================


@pytest.mark.asyncio
async def test_out_of_stock_falls_back_to_another_supplier(db, customer):
    """SN-AX15-0002 needs BAT-AX15-A, which SUP-A has zero of."""
    txn, result = await _run(
        db, customer, order_ref="ORD-100006", serial_number="SN-AX15-0002"
    )

    supplier_decisions = list(db.scalars(
        select(DecisionRecord).where(
            DecisionRecord.transaction_id == txn.id,
            DecisionRecord.decision_type == "SUPPLIER_SELECTION",
        )
    ))
    verdicts = [d.verdict for d in supplier_decisions]
    assert "OUT_OF_STOCK" in verdicts, f"expected a stockout, saw {verdicts}"
    assert "SELECTED" in verdicts, "expected a fallback supplier to be chosen"

    recoveries = _recoveries(db, txn)
    assert any(r.strategy == RecoveryStrategy.FALLBACK_PROVIDER.value
               for r in recoveries)
    assert result.final_state is TransactionState.CLOSED

    # The fallback supplier, not the one that was out of stock, holds the part.
    from servicemesh.core.models import Provider

    chosen = db.get(Provider, txn.selected_supplier_id)
    assert chosen.code != "SUP-A"


# ===========================================================================
# Scenario: permanent failure must never be retried
# ===========================================================================


@pytest.mark.asyncio
async def test_permanent_provider_failure_is_not_retried_forever(
    db, customer, simulators
):
    simulators["marketplace"].set_mode("PERMANENT_FAILURE")

    txn, result = await _run(db, customer)

    assert result.final_state is TransactionState.ESCALATED
    ops = _ops(db, txn, OperationType.VERIFY_PURCHASE)
    total_attempts = sum(o.attempt_count for o in ops)
    # A permanent 500 is classified PERMANENT, so it escalates immediately
    # rather than burning the retry budget.
    assert total_attempts <= 2, f"permanent failure was retried {total_attempts} times"


@pytest.mark.asyncio
async def test_unknown_serial_terminates_without_retry(db, customer):
    txn, result = await _run(
        db, customer, order_ref="ORD-100001", serial_number="SN-DOES-NOT-EXIST"
    )
    assert result.final_state is TransactionState.REJECTED
    ops = _ops(db, txn, OperationType.VERIFY_PURCHASE)
    assert sum(o.attempt_count for o in ops) == 1
