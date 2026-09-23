"""Adapter for the Parts Supplier organization.

Source contract : API key as a query parameter, `{"ok":..,"data":{..}}`
                  envelope, `Idempotency-Key` header on mutations.
Target contract : CHECK_PART_AVAILABILITY, RESERVE_PART, RELEASE_PART,
                  GET_OPERATION_STATUS.

GET_OPERATION_STATUS is the reconciliation path. When a RESERVE_PART times out,
ServiceMesh does not know whether stock was consumed. Guessing is unacceptable
in both directions - assuming success can strand a customer without a part,
assuming failure can double-book inventory. So the orchestrator asks the
supplier directly, keyed by the idempotency key it already holds.
"""

from __future__ import annotations

from typing import ClassVar

from servicemesh.adapters.base import (
    AdapterResult,
    OperationRequest,
    ProviderAdapter,
    register_adapter,
)
from servicemesh.core.config import get_settings
from servicemesh.core.enums import FailureReason, FailureType, OperationType


class PartsSupplierAdapter(ProviderAdapter):
    adapter_key: ClassVar[str] = "parts_supplier_v3"
    supported_operations: ClassVar[frozenset[OperationType]] = frozenset(
        {
            OperationType.CHECK_PART_AVAILABILITY,
            OperationType.RESERVE_PART,
            OperationType.RELEASE_PART,
            OperationType.GET_OPERATION_STATUS,
            OperationType.DISPATCH_PART,
        }
    )

    def _params(self, extra: dict | None = None) -> dict:
        params = {"api_key": get_settings().supplier_api_key}
        params.update(extra or {})
        return params

    async def execute(self, request: OperationRequest) -> AdapterResult:
        match request.operation:
            case OperationType.CHECK_PART_AVAILABILITY:
                return await self._availability(request)
            case OperationType.RESERVE_PART:
                return await self._reserve(request)
            case OperationType.RELEASE_PART:
                return await self._release(request)
            case OperationType.GET_OPERATION_STATUS:
                return await self._status(request)
            case OperationType.DISPATCH_PART:
                return await self._dispatch(request)
        return self._unsupported(request)

    async def _availability(self, request: OperationRequest) -> AdapterResult:
        resp = await self._call(
            request, "GET", "/inventory",
            params=self._params({
                "sku": request.payload["sku"],
                "quantity": request.payload.get("quantity", 1),
                "supplier_code": request.provider.code,
            }),
        )
        if not resp.ok:
            return AdapterResult.from_transport_failure(resp)

        body = resp.json_body()
        data = body.get("data")
        if not isinstance(data, dict) or "available" not in data:
            return AdapterResult.invalid(resp, "supplier inventory response malformed")

        available = bool(data["available"])
        return AdapterResult(
            success=True, status_code=resp.status_code, raw=resp.body,
            latency_ms=resp.latency_ms, request_snapshot=resp.request_snapshot,
            data={
                "available": available,
                # Out of stock is terminal for THIS supplier; the orchestrator
                # should try a different one rather than retry this one.
                "terminal": not available,
                "reason": data.get("reason", ""),
                "sku": data.get("sku"),
                "supplier_code": data.get("supplier_code", request.provider.code),
                "available_quantity": data.get("available_quantity", 0),
                "unit_price": data.get("unit_price", 0.0),
                "lead_time_days": data.get("lead_time_days"),
            },
        )

    async def _reserve(self, request: OperationRequest) -> AdapterResult:
        if not request.idempotency_key:
            return AdapterResult(
                success=False,
                failure_reason=FailureReason.INTERNAL_ERROR,
                failure_type=FailureType.PERMANENT,
                error_message="RESERVE_PART requires an idempotency key",
            )

        resp = await self._call(
            request, "POST", "/reservations",
            json={
                "sku": request.payload["sku"],
                "quantity": request.payload.get("quantity", 1),
                "supplier_code": request.provider.code,
                "external_ref": request.payload.get("external_ref"),
            },
            params=self._params(),
        )
        if not resp.ok:
            if resp.status_code == 409:
                detail = resp.body.get("detail", {}) if isinstance(resp.body, dict) else {}
                return AdapterResult(
                    success=True, status_code=409, raw=resp.body,
                    latency_ms=resp.latency_ms, request_snapshot=resp.request_snapshot,
                    data={
                        "reserved": False, "terminal": True,
                        "reason": "out of stock at this supplier",
                        "available_quantity": detail.get("available_quantity", 0),
                    },
                )
            return AdapterResult.from_transport_failure(resp)

        body = resp.json_body()
        data = body.get("data")
        if not isinstance(data, dict) or "reservation_ref" not in data:
            return AdapterResult.invalid(resp, "supplier reservation response malformed")

        return AdapterResult(
            success=True, status_code=resp.status_code, raw=resp.body,
            latency_ms=resp.latency_ms, request_snapshot=resp.request_snapshot,
            data={
                "reserved": True,
                "terminal": False,
                "reservation_ref": data["reservation_ref"],
                "sku": data.get("sku"),
                "quantity": data.get("quantity", 1),
                "status": data.get("status"),
                "supplier_code": data.get("supplier_code"),
                # True when the supplier recognised the idempotency key and
                # returned the original reservation instead of creating a new
                # one. This is the observable proof that a retry did not
                # double-book, and the dashboard counts it.
                "replayed": bool(data.get("replayed")),
                "expires_at": data.get("expires_at"),
            },
        )

    async def _release(self, request: OperationRequest) -> AdapterResult:
        ref = request.payload["reservation_ref"]
        resp = await self._call(
            request, "POST", f"/reservations/{ref}/release", params=self._params()
        )
        if not resp.ok:
            if resp.status_code == 404:
                # Nothing to compensate - treat as already released.
                return AdapterResult(
                    success=True, status_code=404, raw=resp.body,
                    latency_ms=resp.latency_ms,
                    data={"released": True, "already_released": True,
                          "reason": "reservation not found"},
                )
            return AdapterResult.from_transport_failure(resp)
        data = resp.json_body().get("data", {})
        return AdapterResult(
            success=True, status_code=resp.status_code, raw=resp.body,
            latency_ms=resp.latency_ms, request_snapshot=resp.request_snapshot,
            data={
                "released": True,
                "already_released": bool(data.get("already_released")),
                "reservation_ref": data.get("reservation_ref", ref),
            },
        )


    async def _dispatch(self, request: OperationRequest) -> AdapterResult:
        ref = request.payload["reservation_ref"]
        resp = await self._call(
            request, "POST", f"/reservations/{ref}/dispatch",
            params=self._params(), json={"eta": request.payload.get("eta")},
        )
        if not resp.ok:
            return AdapterResult.from_transport_failure(resp)
        data = resp.json_body().get("data", {})
        return AdapterResult(
            success=True, status_code=resp.status_code, raw=resp.body,
            latency_ms=resp.latency_ms, request_snapshot=resp.request_snapshot,
            data={"dispatched": True, "reservation_ref": data.get("reservation_ref", ref),
                  "status": data.get("dispatch_status", "DISPATCHED"),
                  "eta": data.get("eta")},
        )

    async def _status(self, request: OperationRequest) -> AdapterResult:
        """Answer: did the reservation identified by this key actually land?"""
        key = request.payload.get("idempotency_key") or request.idempotency_key
        if not key:
            return AdapterResult(
                success=False,
                failure_reason=FailureReason.INTERNAL_ERROR,
                failure_type=FailureType.PERMANENT,
                error_message="GET_OPERATION_STATUS requires an idempotency key",
            )
        resp = await self._call(
            request, "GET", f"/reservations/by-key/{key}", params=self._params()
        )
        if not resp.ok:
            return AdapterResult.from_transport_failure(resp)
        data = resp.json_body().get("data", {})
        found = bool(data.get("found"))
        return AdapterResult(
            success=True, status_code=resp.status_code, raw=resp.body,
            latency_ms=resp.latency_ms, request_snapshot=resp.request_snapshot,
            data={
                "found": found,
                "reserved": found,
                "reservation_ref": data.get("reservation_ref"),
                "sku": data.get("sku"),
                "quantity": data.get("quantity", 1),
                "status": data.get("status"),
                "supplier_code": data.get("supplier_code"),
                "replayed": True,
            },
        )


register_adapter(PartsSupplierAdapter())
