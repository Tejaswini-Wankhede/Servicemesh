"""Baseline: conventional sequential point-to-point integration.

What this represents
--------------------
The way cross-company after-sales coordination is commonly implemented today:
a script (or an integration flow) that calls each organization in turn, holds
its progress in memory, and on failure either retries the whole flow or hands
the case to a human. This is the comparison target for the research claims.

It is implemented honestly. It is not a strawman:
  * it retries transient failures (with the same retry budget ServiceMesh uses)
  * it calls the same five organizations through the same adapters
  * it performs the same business steps in the same order

What it deliberately lacks - because these are exactly the properties the
ServiceMesh contribution claims - are:
  1. **Persisted state.** Progress lives in memory. A failure that exhausts
     retries loses it, so recovery means re-running from the beginning.
  2. **Stable idempotency keys.** The organizations require an
     `Idempotency-Key` header on mutating calls, so the baseline sends one -
     but generates a *fresh* key per attempt, as a naive client does. The key
     is therefore present and useless, and a retry creates a genuine second
     reservation or booking at the organization. Duplicates are counted from
     the organization's own database, not assumed.
  3. **Failure classification.** A failure is a failure; permanent business
     rejections are retried alongside transient outages.
  4. **Fallback selection.** One provider is chosen up front; if it cannot
     deliver, the flow fails rather than substituting another.
  5. **Compensation.** Side effects from a partially completed run are left
     behind.

Every metric it reports is measured from its own execution, not assumed.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from servicemesh.adapters.base import (
    OperationRequest,
    ProviderRef,
    get_adapter,
    load_builtin_adapters,
)
from servicemesh.core.config import get_settings
from servicemesh.core.enums import OperationType
from servicemesh.core.models import Component, Customer, Provider
from servicemesh.engines.compatibility import CompatibilityEngine

# The baseline uses the same adapters as ServiceMesh, so the comparison isolates
# orchestration strategy rather than integration quality.
load_builtin_adapters()


@dataclass
class BaselineResult:
    completed: bool
    outcome: str
    reason: str = ""
    api_calls: int = 0
    duplicate_side_effects: int = 0
    workflow_restarts: int = 0
    manual_intervention: bool = False
    elapsed_seconds: float = 0.0
    steps_completed: int = 0
    orphaned_side_effects: list[str] = field(default_factory=list)
    retries: int = 0


class SequentialBaseline:
    """In-memory, restart-on-failure orchestration across the same five orgs."""

    #: The same happy path ServiceMesh walks, so step counts are comparable.
    STEPS = (
        "VERIFY_PURCHASE", "VERIFY_PRODUCT", "VERIFY_WARRANTY", "CHECK_COVERAGE",
        "SELECT_PROVIDER", "VALIDATE_COMPONENT", "CHECK_AVAILABILITY",
        "RESERVE_PART", "BOOK_SERVICE", "COMPLETE_REPAIR",
    )

    def __init__(self, db: Session, max_restarts: int = 2) -> None:
        self.db = db
        self.max_restarts = max_restarts
        self.compat = CompatibilityEngine(db)

    async def run(
        self, customer: Customer, order_ref: str, serial_number: str, issue_type: str
    ) -> BaselineResult:
        started = time.perf_counter()
        result = BaselineResult(completed=False, outcome="FAILED")

        for attempt in range(self.max_restarts + 1):
            if attempt > 0:
                result.workflow_restarts += 1
                # In-memory progress was lost, so every step runs again. The
                # mutating ones therefore duplicate their side effects.
                result.steps_completed = 0

            ok = await self._attempt(customer, order_ref, serial_number,
                                     issue_type, result)
            if ok:
                result.completed = True
                result.outcome = "RESOLVED"
                break
            if result.outcome == "REJECTED":
                break  # business rejection; restarting will not help

        if not result.completed and result.outcome != "REJECTED":
            # Nothing automated is left to try.
            result.manual_intervention = True
            result.outcome = "MANUAL_INTERVENTION"

        result.elapsed_seconds = round(time.perf_counter() - started, 4)
        return result

    # ------------------------------------------------------------------
    async def _attempt(
        self,
        customer: Customer,
        order_ref: str,
        serial_number: str,
        issue_type: str,
        result: BaselineResult,
    ) -> bool:
        mkt = self._provider("MARKETPLACE")
        oem = self._provider("MANUFACTURER")
        wty = self._provider("WARRANTY")

        # --- 1. purchase ------------------------------------------------
        r = await self._call(
            mkt, OperationType.VERIFY_PURCHASE,
            {"order_ref": order_ref, "serial_number": serial_number}, result
        )
        if r is None or not r.get("verified"):
            result.reason = (r or {}).get("reason", "purchase verification failed")
            if r is not None:
                result.outcome = "REJECTED"
            return False
        result.steps_completed += 1

        # --- 2. product -------------------------------------------------
        r = await self._call(
            oem, OperationType.VERIFY_PRODUCT, {"serial_number": serial_number}, result
        )
        if r is None or not r.get("verified"):
            result.reason = (r or {}).get("reason", "product verification failed")
            if r is not None:
                result.outcome = "REJECTED"
            return False
        product = r
        result.steps_completed += 1

        # --- 3. warranty ------------------------------------------------
        r = await self._call(
            wty, OperationType.VERIFY_WARRANTY, {"serial_number": serial_number}, result
        )
        if r is None or not r.get("valid"):
            result.reason = (r or {}).get("reason", "warranty verification failed")
            if r is not None:
                result.outcome = "REJECTED"
            return False
        contract_no = r.get("contract_no")
        result.steps_completed += 1

        component_type = self.compat.required_component_type(issue_type) or "BATTERY"

        # --- 4. coverage ------------------------------------------------
        r = await self._call(wty, OperationType.CHECK_COVERAGE, {
            "contract_no": contract_no, "component_type": component_type,
            "issue_type": issue_type, "estimated_cost": 4000.0,
            "region": customer.region,
        }, result)
        if r is None or not r.get("covered"):
            result.reason = (r or {}).get("reason", "coverage check failed")
            if r is not None:
                result.outcome = "REJECTED"
            return False
        result.steps_completed += 1

        # --- 5. provider: first match wins, no scoring, no fallback -----
        model = self.compat.model_by_code(product.get("model_code", ""))
        centre = self._first_provider("SERVICE_CENTRE", customer.region)
        if centre is None:
            result.reason = "no service centre configured"
            return False
        auth = await self._call(oem, OperationType.CHECK_PROVIDER_AUTHORIZATION, {
            "partner_code": centre.code,
            "category": product.get("category", "LAPTOP"),
            "region": customer.region,
        }, result)
        if auth is None or not auth.get("authorized"):
            # No alternative is attempted - this is the gap being measured.
            result.reason = (auth or {}).get("reason", "provider not authorized")
            return False
        result.steps_completed += 1

        # --- 6. component ------------------------------------------------
        if model is None:
            result.reason = "unknown model"
            return False
        candidates = self.compat.find_compatible(
            model.id, component_type,
            context={"bios_version": product.get("bios_version"),
                     "region": customer.region},
        )
        if not candidates:
            result.reason = f"no compatible {component_type}"
            return False
        component = self.db.get(Component, candidates[0].component_id)
        result.steps_completed += 1

        # --- 7. availability: first supplier only ------------------------
        supplier = self._first_provider("PARTS_SUPPLIER", customer.region)
        if supplier is None:
            result.reason = "no supplier configured"
            return False
        r = await self._call(supplier, OperationType.CHECK_PART_AVAILABILITY,
                             {"sku": component.sku, "quantity": 1}, result)
        if r is None or not r.get("available"):
            # No fallback supplier is tried.
            result.reason = (r or {}).get("reason", "part unavailable")
            return False
        result.steps_completed += 1

        # --- 8. reserve: NO idempotency key ------------------------------
        r = await self._call(supplier, OperationType.RESERVE_PART, {
            "sku": component.sku, "quantity": 1, "external_ref": "baseline",
        }, result, mutating=True)
        if r is None or not r.get("reserved"):
            result.reason = (r or {}).get("reason", "reservation failed")
            return False
        result.orphaned_side_effects.append(f"reservation:{r.get('reservation_ref')}")
        result.steps_completed += 1

        # --- 9. book: NO idempotency key ---------------------------------
        r = await self._call(centre, OperationType.BOOK_SERVICE, {
            "serial_number": serial_number, "issue_type": issue_type,
            "component_sku": component.sku, "external_ref": "baseline",
        }, result, mutating=True)
        if r is None or not r.get("booked"):
            result.reason = (r or {}).get("reason", "booking failed")
            return False
        booking_ref = r.get("booking_ref")
        result.orphaned_side_effects.append(f"booking:{booking_ref}")
        result.steps_completed += 1

        # --- 10. repair --------------------------------------------------
        for status in ("REPAIRING", "COMPLETED"):
            r = await self._call(centre, OperationType.UPDATE_REPAIR, {
                "booking_ref": booking_ref, "status": status, "notes": "baseline",
            }, result, mutating=True)
            if r is None:
                result.reason = "repair update failed"
                return False
        result.steps_completed += 1

        # Completed: the side effects are intended, not orphaned.
        result.orphaned_side_effects.clear()
        return True

    # ------------------------------------------------------------------
    async def _call(
        self,
        provider: Provider,
        operation: OperationType,
        payload: dict,
        result: BaselineResult,
        mutating: bool = False,
    ) -> dict | None:
        """Call an organization, retrying blindly on any failure.

        No idempotency key is sent, and no distinction is drawn between a
        transient outage and a permanent rejection - both are simply retried
        until the budget is gone.
        """
        settings = get_settings()
        adapter = get_adapter(provider.adapter_key)
        max_attempts = settings.default_max_retries

        for attempt in range(1, max_attempts + 1):
            result.api_calls += 1
            if attempt > 1:
                result.retries += 1

            adapter_result = await adapter.execute(OperationRequest(
                operation=operation,
                provider=ProviderRef(
                    id=provider.id, code=provider.code, kind=provider.kind,
                    base_url=provider.base_url, adapter_key=provider.adapter_key,
                    region=provider.region,
                ),
                payload=payload,
                correlation_id="baseline",
                # A fresh key per attempt: syntactically valid, semantically
                # useless. The organization cannot recognise the replay, so the
                # retry produces a second real side effect. This is the defining
                # omission being measured.
                idempotency_key=(str(uuid.uuid4()) if mutating else None),
            ))
            if adapter_result.success:
                return adapter_result.data

        return None

    def _provider(self, kind: str) -> Provider:
        return self.db.scalar(
            select(Provider).where(Provider.kind == kind).limit(1)
        )

    def _first_provider(self, kind: str, region: str) -> Provider | None:
        """No scoring, no constraint filtering - whichever row comes first."""
        return self.db.scalar(
            select(Provider)
            .where(Provider.kind == kind, Provider.region == region)
            .order_by(Provider.code)
            .limit(1)
        )
