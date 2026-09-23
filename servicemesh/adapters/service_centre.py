"""Adapter for the Authorized Service Centre organization.

Source contract : `X-Partner-Key` header, `{"result":..,"data":{..}}` envelope,
                  `Idempotency-Key` on booking creation and cancellation.
Target contract : BOOK_SERVICE, UPDATE_REPAIR, CONFIRM_COMPLETION,
                  CANCEL_SERVICE, GET_OPERATION_STATUS.
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


class ServiceCentreAdapter(ProviderAdapter):
    adapter_key: ClassVar[str] = "service_centre_v3"
    supported_operations: ClassVar[frozenset[OperationType]] = frozenset(
        {
            OperationType.BOOK_SERVICE,
            OperationType.UPDATE_REPAIR,
            OperationType.CONFIRM_COMPLETION,
            OperationType.CANCEL_SERVICE,
            OperationType.GET_OPERATION_STATUS,
        }
    )

    def _auth(self) -> dict:
        return {"X-Partner-Key": get_settings().service_centre_key}

    async def execute(self, request: OperationRequest) -> AdapterResult:
        match request.operation:
            case OperationType.BOOK_SERVICE:
                return await self._book(request)
            case OperationType.UPDATE_REPAIR:
                return await self._update(request, request.payload["status"])
            case OperationType.CONFIRM_COMPLETION:
                return await self._confirm(request)
            case OperationType.CANCEL_SERVICE:
                return await self._cancel(request)
            case OperationType.GET_OPERATION_STATUS:
                return await self._status(request)
        return self._unsupported(request)

    async def _book(self, request: OperationRequest) -> AdapterResult:
        if not request.idempotency_key:
            return AdapterResult(
                success=False,
                failure_reason=FailureReason.INTERNAL_ERROR,
                failure_type=FailureType.PERMANENT,
                error_message="BOOK_SERVICE requires an idempotency key",
            )

        resp = await self._call(
            request, "POST", "/v3/bookings",
            json={
                "partner_code": request.provider.code,
                "serial_number": request.payload["serial_number"],
                "issue_type": request.payload["issue_type"],
                "component_sku": request.payload.get("component_sku"),
                "external_ref": request.payload.get("external_ref"),
            },
            headers=self._auth(),
        )
        if not resp.ok:
            if resp.status_code == 409:
                detail = resp.body.get("detail", {}) if isinstance(resp.body, dict) else {}
                return AdapterResult(
                    success=True, status_code=409, raw=resp.body,
                    latency_ms=resp.latency_ms, request_snapshot=resp.request_snapshot,
                    data={
                        "booked": False, "terminal": True,
                        "reason": detail.get("message", "service centre cannot accept booking"),
                        "code": detail.get("code"),
                    },
                )
            return AdapterResult.from_transport_failure(resp)

        data = resp.json_body().get("data")
        if not isinstance(data, dict) or "booking_ref" not in data:
            return AdapterResult.invalid(resp, "service centre booking response malformed")

        return AdapterResult(
            success=True, status_code=resp.status_code, raw=resp.body,
            latency_ms=resp.latency_ms, request_snapshot=resp.request_snapshot,
            data={
                "booked": True, "terminal": False,
                "booking_ref": data["booking_ref"],
                "status": data.get("status"),
                "technician": data.get("technician"),
                "scheduled_for": data.get("scheduled_for"),
                "replayed": bool(data.get("replayed")),
            },
        )

    async def _update(self, request: OperationRequest, status: str) -> AdapterResult:
        ref = request.payload["booking_ref"]
        resp = await self._call(
            request, "PATCH", f"/v3/bookings/{ref}/status",
            json={
                "status": status,
                "technician": request.payload.get("technician"),
                "notes": request.payload.get("notes"),
            },
            headers=self._auth(),
        )
        if not resp.ok:
            return AdapterResult.from_transport_failure(resp)
        data = resp.json_body().get("data")
        if not isinstance(data, dict):
            return AdapterResult.invalid(resp, "service centre update response malformed")
        return AdapterResult(
            success=True, status_code=resp.status_code, raw=resp.body,
            latency_ms=resp.latency_ms, request_snapshot=resp.request_snapshot,
            data={
                "updated": True, "terminal": False,
                "booking_ref": data.get("booking_ref", ref),
                "status": data.get("status"),
                "technician": data.get("technician"),
                "notes": data.get("diagnosis_notes"),
                "completed_at": data.get("completed_at"),
            },
        )

    async def _confirm(self, request: OperationRequest) -> AdapterResult:
        """Verify completion by reading the booking back, not by asserting it.

        Independent verification matters: ServiceMesh should close a
        transaction because the service centre's own record says COMPLETED,
        not because ServiceMesh previously sent a COMPLETED update.
        """
        ref = request.payload["booking_ref"]
        resp = await self._call(
            request, "GET", f"/v3/bookings/{ref}", headers=self._auth()
        )
        if not resp.ok:
            return AdapterResult.from_transport_failure(resp)
        data = resp.json_body().get("data")
        if not isinstance(data, dict) or "status" not in data:
            return AdapterResult.invalid(resp, "service centre booking read malformed")

        completed = data.get("status") == "COMPLETED"
        return AdapterResult(
            success=True, status_code=resp.status_code, raw=resp.body,
            latency_ms=resp.latency_ms, request_snapshot=resp.request_snapshot,
            data={
                "confirmed": completed,
                "terminal": False,
                "reason": (
                    "service centre confirms repair completed" if completed
                    else f"booking is in state {data.get('status')}, not COMPLETED"
                ),
                "booking_ref": data.get("booking_ref", ref),
                "status": data.get("status"),
                "technician": data.get("technician"),
                "notes": data.get("diagnosis_notes"),
                "completed_at": data.get("completed_at"),
            },
        )

    async def _cancel(self, request: OperationRequest) -> AdapterResult:
        ref = request.payload["booking_ref"]
        resp = await self._call(
            request, "POST", f"/v3/bookings/{ref}/cancel", headers=self._auth()
        )
        if not resp.ok:
            if resp.status_code == 404:
                return AdapterResult(
                    success=True, status_code=404, raw=resp.body,
                    latency_ms=resp.latency_ms,
                    data={"cancelled": True, "already_cancelled": True,
                          "reason": "booking not found"},
                )
            return AdapterResult.from_transport_failure(resp)
        data = resp.json_body().get("data", {})
        return AdapterResult(
            success=True, status_code=resp.status_code, raw=resp.body,
            latency_ms=resp.latency_ms, request_snapshot=resp.request_snapshot,
            data={
                "cancelled": True,
                "already_cancelled": bool(data.get("already_cancelled")),
                "booking_ref": data.get("booking_ref", ref),
            },
        )

    async def _status(self, request: OperationRequest) -> AdapterResult:
        key = request.payload.get("idempotency_key") or request.idempotency_key
        if not key:
            return AdapterResult(
                success=False,
                failure_reason=FailureReason.INTERNAL_ERROR,
                failure_type=FailureType.PERMANENT,
                error_message="GET_OPERATION_STATUS requires an idempotency key",
            )
        resp = await self._call(
            request, "GET", f"/v3/bookings/by-key/{key}", headers=self._auth()
        )
        if not resp.ok:
            return AdapterResult.from_transport_failure(resp)
        data = resp.json_body().get("data", {})
        found = bool(data.get("found"))
        return AdapterResult(
            success=True, status_code=resp.status_code, raw=resp.body,
            latency_ms=resp.latency_ms, request_snapshot=resp.request_snapshot,
            data={
                "found": found, "booked": found,
                "booking_ref": data.get("booking_ref"),
                "status": data.get("status"),
                "technician": data.get("technician"),
                "scheduled_for": data.get("scheduled_for"),
                "replayed": True,
            },
        )


register_adapter(ServiceCentreAdapter())
