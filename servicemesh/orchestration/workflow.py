"""Workflow: the ordered steps of a Service Transaction, and the drive loop.

Each step is a pure function of persisted state: given a transaction sitting at
state X, the step for X knows what to do, does it, and transitions to X+1. That
property is what makes `drive()` safe to call repeatedly - after a crash, after
a timeout, after an operator clicks "resume", days later. It always computes the
next action from the database, never from in-memory context.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC

from sqlalchemy import select
from sqlalchemy.orm import Session

from servicemesh.core.config import get_settings
from servicemesh.core.enums import (
    FailureReason,
    FailureType,
    OperationType,
    ProviderKind,
    TransactionState,
)
from servicemesh.core.models import Component, Provider, ServiceTransaction, utcnow
from servicemesh.orchestration.orchestrator import DriveResult, Orchestrator, StepOutcome

logger = logging.getLogger("servicemesh.workflow")

MAX_STEPS = 40  # guard against an unexpected loop; far above the ~13 real steps


class Workflow:
    def __init__(self, db: Session, **kwargs) -> None:
        self.db = db
        self.orc = Orchestrator(db, **kwargs)
        self.sm = self.orc.sm

    # =====================================================================
    # Drive loop
    # =====================================================================

    async def drive(
        self, txn: ServiceTransaction, *, auto_repair: bool = False, max_steps: int = MAX_STEPS
    ) -> DriveResult:
        """Advance the transaction as far as it can currently go."""
        steps = 0
        recoveries = 0
        halted = ""

        while steps < max_steps:
            state = TransactionState(txn.state)

            if state in {TransactionState.CLOSED, TransactionState.FAILED,
                         TransactionState.REJECTED, TransactionState.CANCELLED}:
                halted = f"terminal state {state.value}"
                break
            if state is TransactionState.ESCALATED:
                halted = "escalated; awaiting operator"
                break
            if state is TransactionState.WAITING:
                halted = "waiting for an external organization to act"
                break

            handler = self._handler_for(state, auto_repair=auto_repair)
            if handler is None:
                halted = f"no handler for state {state.value}"
                break

            steps += 1
            outcome = await handler(txn)
            self.db.flush()

            if outcome.ok:
                continue

            if outcome.halt:
                halted = outcome.note
                break

            if outcome.failure is None:
                halted = outcome.note or "step failed without a failure context"
                break

            # Failure -> record, decide, apply.
            provider = (
                self.db.get(Provider, outcome.failure.provider_id)
                if outcome.failure.provider_id else None
            )
            from servicemesh.core.models import ProviderOperation as _Op

            op = (
                self.db.get(_Op, outcome.failure.operation_id)
                if outcome.failure.operation_id else self._latest_operation(txn)
            )
            failure = self.orc.record_failure(
                txn, op, provider,
                failure_type=outcome.failure.failure_type,
                failure_reason=outcome.failure.failure_reason,
                message=outcome.failure.message,
            )
            recoveries += 1
            keep_going = await self.orc.apply_recovery(txn, outcome.failure, failure)
            self.db.flush()
            if not keep_going:
                halted = f"recovery halted the transaction at {txn.state}"
                break

        if steps >= max_steps:
            halted = "step limit reached"

        self._evaluate_sla(txn)
        self.db.flush()
        return DriveResult(
            transaction_id=txn.id,
            final_state=TransactionState(txn.state),
            steps_executed=steps,
            recoveries_applied=recoveries,
            halted_reason=halted,
        )

    def _handler_for(
        self, state: TransactionState, *, auto_repair: bool
    ) -> Callable[[ServiceTransaction], Awaitable[StepOutcome]] | None:
        mapping = {
            TransactionState.CREATED: self.step_verify_purchase,
            TransactionState.PURCHASE_VERIFIED: self.step_verify_product,
            TransactionState.PRODUCT_VERIFIED: self.step_verify_warranty,
            TransactionState.WARRANTY_VERIFIED: self.step_check_coverage,
            TransactionState.COVERAGE_CHECKED: self.step_select_provider,
            TransactionState.PROVIDER_SELECTED: self.step_validate_component,
            TransactionState.COMPONENT_VALIDATED: self.step_request_part,
            TransactionState.PART_REQUESTED: self.step_reserve_part,
            TransactionState.PART_CONFIRMED: self.step_book_service,
            TransactionState.REPAIR_COMPLETED: self.step_confirm_completion,
            TransactionState.SERVICE_VERIFIED: self.step_close,
        }
        if auto_repair:
            mapping[TransactionState.REPAIR_SCHEDULED] = self.step_start_repair
            mapping[TransactionState.REPAIR_IN_PROGRESS] = self.step_complete_repair
        else:
            mapping[TransactionState.REPAIR_SCHEDULED] = self.step_await_repair
            mapping[TransactionState.REPAIR_IN_PROGRESS] = self.step_await_repair
        return mapping.get(state)

    # =====================================================================
    # Steps
    # =====================================================================

    async def step_verify_purchase(self, txn: ServiceTransaction) -> StepOutcome:
        provider = self._singleton(ProviderKind.MARKETPLACE)
        if provider is None:
            return self._no_provider(txn, ProviderKind.MARKETPLACE)

        op, result = await self.orc.execute_operation(
            txn, OperationType.VERIFY_PURCHASE, provider,
            {"order_ref": txn.order_ref, "serial_number": txn.serial_number},
        )
        if (
            not result.success
            and not (
                get_settings().allow_demo_external_records
                and get_settings().environment == "local"
            )
        ):
            return self._failed(txn, provider, result, OperationType.VERIFY_PURCHASE, op)

        txn.purchase_evidence = result.data or {}
        if not result.success or not (result.data or {}).get("verified"):
            if get_settings().allow_demo_external_records and get_settings().environment == "local":
                txn.purchase_evidence = {
                    "verified": True, "reason": "demo purchase reference accepted",
                    "order_ref": txn.order_ref, "serial_number": txn.serial_number,
                    "model_code": "AX14-PRO-2023", "unit_price": 84999.0,
                    "purchased_at": utcnow().isoformat(),
                    "buyer_email": txn.customer.email, "buyer_name": txn.customer.full_name,
                    "buyer_region": txn.customer.region, "buyer_city": txn.customer.city,
                }
                self.sm.advance(
                    txn, reason="demo purchase reference accepted",
                    context={"order_ref": txn.order_ref, "model_code": "AX14-PRO-2023"},
                )
                return StepOutcome(True)
            return self._reject(
                txn, "PURCHASE_VERIFICATION",
                result.data.get("reason", "purchase could not be verified"),
            )

        self.sm.advance(
            txn, reason="marketplace confirmed the purchase record",
            context={"order_ref": txn.order_ref,
                     "model_code": result.data.get("model_code")},
        )
        return StepOutcome(True)

    async def step_verify_product(self, txn: ServiceTransaction) -> StepOutcome:
        provider = self._singleton(ProviderKind.MANUFACTURER)
        if provider is None:
            return self._no_provider(txn, ProviderKind.MANUFACTURER)

        op, result = await self.orc.execute_operation(
            txn, OperationType.VERIFY_PRODUCT, provider,
            {"serial_number": txn.serial_number},
        )
        if (
            not result.success
            and not (
                get_settings().allow_demo_external_records
                and get_settings().environment == "local"
            )
        ):
            return self._failed(txn, provider, result, OperationType.VERIFY_PRODUCT, op)

        txn.product_evidence = result.data or {}
        if not result.success or not (result.data or {}).get("verified"):
            if get_settings().allow_demo_external_records and get_settings().environment == "local":
                txn.product_evidence = {
                    "verified": True, "reason": "demo device serial accepted",
                    "serial_number": txn.serial_number, "model_code": "AX14-PRO-2023",
                    "model_name": "Demo AX14 Pro", "manufacturer_code": "ACME",
                    "category": "LAPTOP", "approved_component_skus": [],
                    "standard_warranty_months": 24,
                }
                model = self.orc.compat.model_by_code("AX14-PRO-2023")
                if model is not None:
                    txn.product_model_id = model.id
                txn.required_component_type = self.orc.compat.required_component_type(txn.issue_type)
                self.sm.advance(
                    txn, reason="demo device serial accepted",
                    context={"model_code": "AX14-PRO-2023", "required_component_type": txn.required_component_type},
                )
                return StepOutcome(True)
            return self._reject(
                txn, "PRODUCT_VERIFICATION",
                result.data.get("reason", "serial not verified by the OEM"),
            )

        # Bind the transaction to the catalogue model, and derive which
        # component the reported issue implicates.
        model = self.orc.compat.model_by_code(result.data.get("model_code", ""))
        if model is not None:
            txn.product_model_id = model.id
        txn.required_component_type = self.orc.compat.required_component_type(
            txn.issue_type
        )

        self.sm.advance(
            txn, reason="OEM confirmed the serial number",
            context={
                "model_code": result.data.get("model_code"),
                "bios_version": result.data.get("bios_version"),
                "required_component_type": txn.required_component_type,
            },
        )
        return StepOutcome(True)

    async def step_verify_warranty(self, txn: ServiceTransaction) -> StepOutcome:
        provider = self._singleton(ProviderKind.WARRANTY)
        if provider is None:
            return self._no_provider(txn, ProviderKind.WARRANTY)

        op, result = await self.orc.execute_operation(
            txn, OperationType.VERIFY_WARRANTY, provider,
            {
                "serial_number": txn.serial_number,
                "purchased_at": (txn.purchase_evidence or {}).get("purchased_at"),
            },
        )
        if (
            get_settings().allow_demo_external_records
            and get_settings().environment == "local"
            and (not result.success or not result.data.get("valid"))
        ):
            result.data = {
                "valid": True, "terminal": False, "reason": "demo warranty accepted",
                "status_code": "DEMO-WTY", "contract_no": f"DEMO-{txn.serial_number}",
                "serial_number": txn.serial_number, "plan_code": "DEMO",
                "starts_on": utcnow().date().isoformat(),
                "expires_on": "2099-12-31", "region": txn.customer.region,
                "claims_used": 0, "max_claims": 99,
            }
        elif not result.success:
            return self._failed(txn, provider, result, OperationType.VERIFY_WARRANTY, op)

        txn.warranty_evidence = result.data

        decision = self.orc.policy.evaluate("WARRANTY_ELIGIBILITY", {
            "purchase": txn.purchase_evidence or {},
            "product": txn.product_evidence or {},
            "warranty": result.data,
        })
        self.orc._record_decision(
            txn, "POLICY", "ALLOWED" if decision.allowed else "DENIED",
            engine="policy_engine", rule_version=decision.version,
            reasons=decision.reasons, subject_label="WARRANTY_ELIGIBILITY",
            inputs={"contract_no": result.data.get("contract_no")},
        )

        if not decision.allowed:
            if decision.terminal:
                return self._reject(
                    txn, "WARRANTY_ELIGIBILITY",
                    "; ".join(decision.violation_reasons),
                )
            # Non-terminal violation (e.g. recall) needs a human.
            self.sm.transition(
                txn, TransactionState.ESCALATED,
                reason="; ".join(decision.violation_reasons), actor="POLICY_ENGINE",
            )
            return StepOutcome(False, halt=True, note="policy escalation")

        self.sm.advance(
            txn, reason=f"warranty contract {result.data.get('contract_no')} is active",
            context={"contract_no": result.data.get("contract_no"),
                     "plan_code": result.data.get("plan_code")},
        )
        return StepOutcome(True)

    async def step_check_coverage(self, txn: ServiceTransaction) -> StepOutcome:
        provider = self._singleton(ProviderKind.WARRANTY)
        if provider is None:
            return self._no_provider(txn, ProviderKind.WARRANTY)

        wty = txn.warranty_evidence or {}
        op, result = await self.orc.execute_operation(
            txn, OperationType.CHECK_COVERAGE, provider,
            {
                "contract_no": wty.get("contract_no"),
                "component_type": txn.required_component_type or "UNKNOWN",
                "issue_type": txn.issue_type,
                "estimated_cost": (txn.purchase_evidence or {}).get("unit_price", 0.0) * 0.1,
                "region": txn.customer.region,
            },
        )
        if (
            get_settings().allow_demo_external_records
            and get_settings().environment == "local"
            and (not result.success or not result.data.get("covered"))
        ):
            result.data = {
                "covered": True, "terminal": False, "reason": "demo coverage accepted",
                "status_code": "DEMO-COVERAGE", "contract_no": wty.get("contract_no"),
                "approved_amount": 50000.0, "deductible": 0.0,
                "coverage_limit": 50000.0, "authorization_code": "DEMO-AUTH",
            }
        elif not result.success:
            return self._failed(txn, provider, result, OperationType.CHECK_COVERAGE, op)

        txn.coverage_evidence = result.data
        decision = self.orc.policy.evaluate("COVERAGE", {
            "coverage": result.data,
            "warranty": wty,
            "customer_region": txn.customer.region,
        })
        self.orc._record_decision(
            txn, "POLICY", "ALLOWED" if decision.allowed else "DENIED",
            engine="policy_engine", rule_version=decision.version,
            reasons=decision.reasons, subject_label="COVERAGE",
            inputs={"component_type": txn.required_component_type,
                    "issue_type": txn.issue_type},
        )
        if not decision.allowed:
            return self._reject(txn, "COVERAGE", "; ".join(decision.violation_reasons))

        self.sm.advance(
            txn,
            reason=f"repair approved for {result.data.get('approved_amount')}",
            context={"authorization_code": result.data.get("authorization_code"),
                     "approved_amount": result.data.get("approved_amount")},
        )
        return StepOutcome(True)

    async def step_select_provider(self, txn: ServiceTransaction) -> StepOutcome:
        """Pick a service centre, then verify authorization with the OEM.

        Selection ranks on our own criteria; the OEM has the final word on
        authorization. A provider that wins on score but is not authorized is
        excluded and the next candidate is evaluated - within this single step,
        so the transaction does not bounce through RETRYING for what is a
        normal part of choosing.
        """
        oem = self._singleton(ProviderKind.MANUFACTURER)
        excluded = list(txn.excluded_provider_ids or [])

        for _ in range(5):
            result = self.orc.selection.select(
                ProviderKind.SERVICE_CENTRE.value,
                customer_region=txn.customer.region,
                manufacturer_code=self.orc._manufacturer_code(txn),
                exclude_provider_ids=excluded,
                customer_lat=txn.customer.latitude,
                customer_lon=txn.customer.longitude,
                ml_scorer=self.orc.ml_provider_scorer,
            )
            if not result.found or result.selected is None:
                self.orc._record_decision(
                    txn, "PROVIDER_SELECTION", "NONE_ELIGIBLE",
                    engine="selection_engine", rule_version=result.version,
                    reasons=[result.reason] + [
                        f"{e.code}: {'; '.join(e.reasons)}" for e in result.eliminated
                    ],
                )
                return self._reject(
                    txn, "PROVIDER_SELECTION",
                    "no authorized service provider satisfies the constraints",
                    escalate=True,
                )

            candidate = result.selected
            provider = self.db.get(Provider, candidate.provider_id)

            # Ask the OEM whether this partner is authorized.
            auth_data: dict = {}
            if oem is not None:
                auth_op, auth_result = await self.orc.execute_operation(
                    txn, OperationType.CHECK_PROVIDER_AUTHORIZATION, oem,
                    {
                        "partner_code": provider.code,
                        "category": (txn.product_evidence or {}).get("category", "LAPTOP"),
                        "region": txn.customer.region,
                    },
                )
                if not auth_result.success:
                    return self._failed(
                        txn, oem, auth_result,
                        OperationType.CHECK_PROVIDER_AUTHORIZATION, auth_op,
                    )
                auth_data = auth_result.data

            policy_decision = self.orc.policy.evaluate("PROVIDER_ELIGIBILITY", {
                "provider": {
                    "is_active": provider.is_active,
                    "region": provider.region,
                    "available_capacity": provider.available_capacity,
                    "sla_hours": provider.sla_hours,
                },
                "provider_authorization": auth_data,
                "customer_region": txn.customer.region,
                "max_sla_hours": get_settings().default_sla_hours,
            })

            self.orc._record_decision(
                txn, "PROVIDER_SELECTION",
                "SELECTED" if policy_decision.allowed else "REJECTED",
                engine="selection_engine+policy_engine",
                rule_version=f"{result.version}/{policy_decision.version}",
                subject_id=provider.id, subject_label=provider.code,
                reasons=[result.reason] + policy_decision.reasons,
                inputs={
                    "eliminated": [
                        {"code": e.code, "reasons": e.reasons} for e in result.eliminated
                    ],
                    "oem_authorization": auth_data,
                },
                scores={
                    "weights": result.weights,
                    "winner": {
                        "code": candidate.code, "total": candidate.total_score,
                        "components": candidate.components, "raw": candidate.raw,
                        "ml_risk_score": candidate.ml_risk_score,
                    },
                    "ranking": [
                        {"code": r.code, "total": r.total_score} for r in result.ranked
                    ],
                },
            )

            if policy_decision.allowed:
                txn.selected_service_provider_id = provider.id
                self.sm.advance(
                    txn,
                    reason=(
                        f"selected {provider.code} (score {candidate.total_score}); "
                        f"OEM authorization confirmed"
                    ),
                    context={"provider_code": provider.code,
                             "score": candidate.total_score},
                )
                return StepOutcome(True)

            # Not eligible - exclude and try the next candidate.
            excluded.append(provider.id)
            txn.excluded_provider_ids = list(
                set((txn.excluded_provider_ids or []) + [provider.id])
            )
            logger.info(
                "txn=%s provider %s rejected: %s",
                txn.reference, provider.code, policy_decision.violation_reasons,
            )

        return self._reject(
            txn, "PROVIDER_SELECTION",
            "exhausted candidate service providers without finding an authorized one",
            escalate=True,
        )

    async def step_validate_component(self, txn: ServiceTransaction) -> StepOutcome:
        """Decide which component will be fitted. Deterministic, no network."""
        if not txn.product_model_id or not txn.required_component_type:
            return self._reject(
                txn, "COMPONENT_VALIDATION",
                f"cannot map issue '{txn.issue_type}' to a serviceable component type",
                escalate=True,
            )

        ctx = self.orc._compat_context(txn)
        excluded = list(txn.excluded_component_ids or [])

        # If the customer/technician nominated a specific part, it must be
        # checked first - and rejected explicitly if it is not compatible.
        requested_sku = (txn.nlp_extraction or {}).get("requested_component_sku")
        if requested_sku and requested_sku not in excluded:
            component = self.db.scalar(
                select(Component).where(Component.sku == requested_sku)
            )
            if component is not None:
                check = self.orc.compat.check(txn.product_model_id, component.id, ctx)
                self.orc._record_decision(
                    txn, "COMPATIBILITY", check.verdict.value,
                    engine="compatibility_engine", rule_version=check.rule_version,
                    subject_id=component.id, subject_label=component.sku,
                    reasons=check.reasons,
                    inputs={"requested_by": "customer", **ctx},
                )
                if check.is_compatible:
                    txn.selected_component_id = component.id
                    self.sm.advance(
                        txn, reason=f"requested component {component.sku} is compatible",
                        context={"component_sku": component.sku},
                    )
                    return StepOutcome(True)

                # Rejected. Fall through to automatic selection, and record the
                # rejection as a real failure so recovery logs the substitution.
                txn.excluded_component_ids = list(set(excluded + [component.id]))
                excluded = txn.excluded_component_ids

        candidates = self.orc.compat.find_compatible(
            txn.product_model_id, txn.required_component_type,
            context=ctx, exclude_component_ids=excluded,
        )
        if not candidates:
            self.orc._record_decision(
                txn, "COMPATIBILITY", "NONE_AVAILABLE",
                engine="compatibility_engine",
                reasons=[
                    f"no compatible {txn.required_component_type} found for this model",
                    f"{len(excluded)} component(s) already excluded",
                ],
                inputs=ctx,
            )
            return self._reject(
                txn, "COMPONENT_VALIDATION",
                f"no compatible {txn.required_component_type} is available for this model",
                escalate=True,
            )

        chosen = candidates[0]
        under_warranty = bool((txn.warranty_evidence or {}).get("valid"))
        component = self.db.get(Component, chosen.component_id)
        policy_decision = self.orc.policy.evaluate("COMPONENT_SELECTION", {
            "compatibility": {"verdict": "COMPATIBLE", "reasons": chosen.reasons},
            "component": {"is_oem_certified": component.is_oem_certified},
            "under_warranty": under_warranty,
        })

        self.orc._record_decision(
            txn, "COMPATIBILITY", "COMPATIBLE" if policy_decision.allowed else "REJECTED",
            engine="compatibility_engine+policy_engine",
            subject_id=chosen.component_id, subject_label=chosen.sku,
            reasons=chosen.reasons + policy_decision.reasons,
            inputs=ctx,
            scores={"candidates": [
                {"sku": c.sku, "rank_score": c.rank_score,
                 "oem_certified": c.is_oem_certified} for c in candidates
            ]},
        )

        if not policy_decision.allowed:
            txn.excluded_component_ids = list(set(excluded + [chosen.component_id]))
            return self._failure_outcome(
                txn, None, FailureType.PERMANENT,
                FailureReason.INCOMPATIBLE_COMPONENT,
                "; ".join(policy_decision.violation_reasons),
                OperationType.CHECK_COMPATIBILITY,
            )

        txn.selected_component_id = chosen.component_id
        self.sm.advance(
            txn, reason=f"component {chosen.sku} validated as compatible",
            context={"component_sku": chosen.sku,
                     "candidates_considered": len(candidates)},
        )
        return StepOutcome(True)

    async def step_request_part(self, txn: ServiceTransaction) -> StepOutcome:
        """Choose a supplier and confirm the part is actually in stock."""
        component = self.db.get(Component, txn.selected_component_id)
        if component is None:
            return self._reject(txn, "PART_REQUEST", "no component selected", escalate=True)

        result = self.orc.selection.select(
            ProviderKind.PARTS_SUPPLIER.value,
            customer_region=txn.customer.region,
            manufacturer_code=self.orc._manufacturer_code(txn),
            exclude_provider_ids=list(txn.excluded_provider_ids or []),
            customer_lat=txn.customer.latitude,
            customer_lon=txn.customer.longitude,
            require_capacity=False,
            ml_scorer=self.orc.ml_provider_scorer,
        )
        if not result.found or result.selected is None:
            return self._failure_outcome(
                txn, None, FailureType.PERMANENT, FailureReason.NO_ELIGIBLE_PROVIDER,
                result.reason, OperationType.CHECK_PART_AVAILABILITY,
            )

        supplier = self.db.get(Provider, result.selected.provider_id)
        avail_op, avail = await self.orc.execute_operation(
            txn, OperationType.CHECK_PART_AVAILABILITY, supplier,
            {"sku": component.sku, "quantity": 1},
        )
        if not avail.success:
            return self._failed(
                txn, supplier, avail, OperationType.CHECK_PART_AVAILABILITY, avail_op
            )

        self.orc._record_decision(
            txn, "SUPPLIER_SELECTION",
            "SELECTED" if avail.data.get("available") else "OUT_OF_STOCK",
            engine="selection_engine",
            subject_id=supplier.id, subject_label=supplier.code,
            reasons=[result.reason, avail.data.get("reason", "")],
            scores={"ranking": [
                {"code": r.code, "total": r.total_score} for r in result.ranked
            ]},
            inputs={"sku": component.sku},
        )

        if not avail.data.get("available"):
            # Out of stock here is permanent for THIS supplier -> fallback.
            return self._failure_outcome(
                txn, supplier, FailureType.PERMANENT, FailureReason.OUT_OF_STOCK,
                avail.data.get("reason", "out of stock"),
                OperationType.CHECK_PART_AVAILABILITY, op=avail_op,
            )

        txn.selected_supplier_id = supplier.id
        self.sm.advance(
            txn,
            reason=f"{supplier.code} has {component.sku} in stock",
            context={"supplier_code": supplier.code, "sku": component.sku,
                     "unit_price": avail.data.get("unit_price")},
        )
        return StepOutcome(True)

    async def step_reserve_part(self, txn: ServiceTransaction) -> StepOutcome:
        supplier = self.db.get(Provider, txn.selected_supplier_id)
        component = self.db.get(Component, txn.selected_component_id)
        if supplier is None or component is None:
            return self._reject(
                txn, "PART_RESERVATION", "supplier or component missing", escalate=True
            )

        op, result = await self.orc.execute_operation(
            txn, OperationType.RESERVE_PART, supplier,
            {"sku": component.sku, "quantity": 1, "external_ref": txn.reference},
        )
        if not result.success:
            return self._failed(txn, supplier, result, OperationType.RESERVE_PART, op)

        if not result.data.get("reserved"):
            return self._failure_outcome(
                txn, supplier, FailureType.PERMANENT, FailureReason.OUT_OF_STOCK,
                result.data.get("reason", "reservation refused"),
                OperationType.RESERVE_PART, op=op,
            )

        txn.part_reservation_ref = result.data.get("reservation_ref")
        txn.part_delivery_status = "RESERVED"
        txn.part_price = result.data.get("unit_price")
        self.sm.advance(
            txn,
            reason=(
                f"part reserved ({txn.part_reservation_ref})"
                + (" [idempotent replay: no duplicate reservation created]"
                   if result.data.get("replayed") else "")
            ),
            context={"reservation_ref": txn.part_reservation_ref,
                     "replayed": bool(result.data.get("replayed"))},
        )
        return StepOutcome(True)

    async def step_book_service(self, txn: ServiceTransaction) -> StepOutcome:
        provider = self.db.get(Provider, txn.selected_service_provider_id)
        component = self.db.get(Component, txn.selected_component_id)
        if provider is None:
            return self._reject(
                txn, "SERVICE_BOOKING", "no service provider selected", escalate=True
            )

        op, result = await self.orc.execute_operation(
            txn, OperationType.BOOK_SERVICE, provider,
            {
                "serial_number": txn.serial_number,
                "issue_type": txn.issue_type,
                "component_sku": component.sku if component else None,
                "external_ref": txn.reference,
            },
        )
        if not result.success:
            return self._failed(txn, provider, result, OperationType.BOOK_SERVICE, op)

        if not result.data.get("booked"):
            return self._failure_outcome(
                txn, provider, FailureType.PERMANENT, FailureReason.BUSINESS_REJECTION,
                result.data.get("reason", "booking refused"),
                OperationType.BOOK_SERVICE, op=op,
            )

        txn.service_booking_ref = result.data.get("booking_ref")
        txn.assigned_repair_person = result.data.get("technician")
        provider.capacity_used += 1
        self.sm.advance(
            txn,
            reason=f"service booked at {provider.code} ({txn.service_booking_ref})",
            context={
                "booking_ref": txn.service_booking_ref,
                "technician": result.data.get("technician"),
                "scheduled_for": result.data.get("scheduled_for"),
                "replayed": bool(result.data.get("replayed")),
            },
        )
        return StepOutcome(True)

    async def step_await_repair(self, txn: ServiceTransaction) -> StepOutcome:
        """Real repairs take days. Pause until the service centre reports back.

        The Provider Portal calls `advance_repair()` which moves the
        transaction forward and drives it again.
        """
        self.sm.transition(
            txn, TransactionState.WAITING,
            reason="awaiting repair progress from the service centre",
            actor="ORCHESTRATOR",
        )
        return StepOutcome(False, halt=True, note="awaiting service centre update")

    async def step_start_repair(self, txn: ServiceTransaction) -> StepOutcome:
        provider = self.db.get(Provider, txn.selected_service_provider_id)
        op, result = await self.orc.execute_operation(
            txn, OperationType.UPDATE_REPAIR, provider,
            {"booking_ref": txn.service_booking_ref, "status": "REPAIRING",
             "notes": "repair started"},
        )
        if not result.success:
            return self._failed(txn, provider, result, OperationType.UPDATE_REPAIR, op)
        self.sm.advance(txn, reason="service centre reports repair in progress")
        return StepOutcome(True)

    async def step_complete_repair(self, txn: ServiceTransaction) -> StepOutcome:
        provider = self.db.get(Provider, txn.selected_service_provider_id)
        op, result = await self.orc.execute_operation(
            txn, OperationType.UPDATE_REPAIR, provider,
            {"booking_ref": txn.service_booking_ref, "status": "COMPLETED",
             "notes": "component replaced and tested"},
        )
        if not result.success:
            return self._failed(txn, provider, result, OperationType.UPDATE_REPAIR, op)
        self.sm.advance(txn, reason="service centre reports repair completed")
        return StepOutcome(True)

    async def step_confirm_completion(self, txn: ServiceTransaction) -> StepOutcome:
        """Verify completion by reading the service centre's own record."""
        provider = self.db.get(Provider, txn.selected_service_provider_id)
        op, result = await self.orc.execute_operation(
            txn, OperationType.CONFIRM_COMPLETION, provider,
            {"booking_ref": txn.service_booking_ref},
        )
        if not result.success:
            return self._failed(
                txn, provider, result, OperationType.CONFIRM_COMPLETION, op
            )

        if not result.data.get("confirmed"):
            return self._failure_outcome(
                txn, provider, FailureType.TRANSIENT, FailureReason.BUSINESS_REJECTION,
                result.data.get("reason", "service centre has not confirmed completion"),
                OperationType.CONFIRM_COMPLETION, op=op,
            )

        txn.verification_result = "VERIFIED"
        txn.repair_notes = txn.repair_notes or "Repair completed and independently verified."
        self.sm.advance(
            txn, reason="independent verification: service centre record shows COMPLETED",
            context={"completed_at": result.data.get("completed_at"),
                     "technician": result.data.get("technician")},
        )
        return StepOutcome(True)

    async def step_close(self, txn: ServiceTransaction) -> StepOutcome:
        provider = self.db.get(Provider, txn.selected_service_provider_id)
        if provider is not None and provider.capacity_used > 0:
            provider.capacity_used -= 1
        txn.outcome = "RESOLVED"
        txn.outcome_reason = "repair completed and verified across all participants"
        self.sm.advance(txn, reason="all participants completed; closing transaction")
        return StepOutcome(True)

    # =====================================================================
    # Helpers
    # =====================================================================

    def _singleton(self, kind: ProviderKind) -> Provider | None:
        return self.db.scalar(
            select(Provider).where(
                Provider.kind == kind.value, Provider.is_active.is_(True)
            ).limit(1)
        )

    def _no_provider(self, txn: ServiceTransaction, kind: ProviderKind) -> StepOutcome:
        return self._reject(
            txn, "PROVIDER_REGISTRY",
            f"no active {kind.value} provider is registered", escalate=True,
        )

    def _failed(
        self,
        txn: ServiceTransaction,
        provider: Provider,
        result,
        operation: OperationType,
        op=None,
    ) -> StepOutcome:
        return self._failure_outcome(
            txn, provider,
            result.failure_type or FailureType.TRANSIENT,
            result.failure_reason or FailureReason.INTERNAL_ERROR,
            result.error_message or "provider call failed",
            operation,
            op=op,
        )

    def _failure_outcome(
        self,
        txn: ServiceTransaction,
        provider: Provider | None,
        failure_type: FailureType,
        failure_reason: FailureReason,
        message: str,
        operation: OperationType,
        provider_declared_terminal: bool = False,
        op=None,
    ) -> StepOutcome:
        # `op` is the operation that actually failed. Falling back to "latest
        # by sequence" is wrong whenever provider selection alternates between
        # candidates, because the newest row then belongs to a different
        # provider and its attempt count resets the retry budget.
        ctx = self.orc.build_failure_context(
            txn, op if op is not None else self._latest_operation(txn), provider,
            failure_type=failure_type, failure_reason=failure_reason,
            message=message, operation_type=operation,
            provider_declared_terminal=provider_declared_terminal,
        )
        return StepOutcome(False, failure=ctx)

    def _reject(
        self, txn: ServiceTransaction, stage: str, reason: str, escalate: bool = False
    ) -> StepOutcome:
        """Terminate cleanly with a customer-readable explanation."""
        txn.outcome = "ESCALATED" if escalate else "REJECTED"
        txn.outcome_reason = reason
        target = TransactionState.ESCALATED if escalate else TransactionState.REJECTED
        self.sm.transition(
            txn, target, reason=f"{stage}: {reason}", actor="ORCHESTRATOR",
            context={"stage": stage},
        )
        return StepOutcome(False, halt=True, note=f"{stage}: {reason}")

    def _latest_operation(self, txn: ServiceTransaction):
        from servicemesh.core.models import ProviderOperation

        return self.db.scalar(
            select(ProviderOperation)
            .where(ProviderOperation.transaction_id == txn.id)
            .order_by(ProviderOperation.sequence.desc())
            .limit(1)
        )

    def _evaluate_sla(self, txn: ServiceTransaction) -> None:
        if txn.sla_due_at is None:
            return
        due = txn.sla_due_at
        if due.tzinfo is None:

            due = due.replace(tzinfo=UTC)
        reference = txn.closed_at or utcnow()
        if reference.tzinfo is None:

            reference = reference.replace(tzinfo=UTC)
        breached = reference > due
        if breached and not txn.sla_breached:
            txn.sla_breached = True
            if txn.selected_service_provider_id:
                provider = self.db.get(Provider, txn.selected_service_provider_id)
                if provider is not None:
                    provider.sla_violations += 1
