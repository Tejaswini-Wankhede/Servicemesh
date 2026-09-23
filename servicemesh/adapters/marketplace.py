"""Adapter for the Marketplace organization.

Source contract : snake_case REST, `X-API-Key`, flat JSON.
Target contract : ServiceMesh VERIFY_PURCHASE normalised shape.
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
from servicemesh.core.enums import OperationType


class MarketplaceAdapter(ProviderAdapter):
    adapter_key: ClassVar[str] = "marketplace_v1"
    supported_operations: ClassVar[frozenset[OperationType]] = frozenset(
        {OperationType.VERIFY_PURCHASE}
    )

    async def execute(self, request: OperationRequest) -> AdapterResult:
        if request.operation is not OperationType.VERIFY_PURCHASE:
            return self._unsupported(request)

        settings = get_settings()
        resp = await self._call(
            request, "POST", "/api/v1/orders/lookup",
            json={
                "order_reference": request.payload["order_ref"],
                "serial_number": request.payload.get("serial_number"),
            },
            headers={"X-API-Key": settings.marketplace_api_key},
        )

        if not resp.ok:
            # A 409 here means "that serial is not in that order" - a real
            # business answer, not an outage. Surface it as a terminal
            # rejection so the orchestrator stops instead of retrying.
            if resp.status_code in (404, 409):
                return AdapterResult(
                    success=True, status_code=resp.status_code, raw=resp.body,
                    latency_ms=resp.latency_ms, request_snapshot=resp.request_snapshot,
                    data={
                        "verified": False,
                        "terminal": True,
                        "reason": resp.error_message or "purchase record not found",
                    },
                )
            return AdapterResult.from_transport_failure(resp)

        body = resp.json_body()
        if "order_reference" not in body or "items" not in body:
            return AdapterResult.invalid(
                resp, "marketplace response missing order_reference/items"
            )

        items = body.get("items") or []
        if not items:
            return AdapterResult(
                success=True, status_code=resp.status_code, raw=resp.body,
                latency_ms=resp.latency_ms, request_snapshot=resp.request_snapshot,
                data={"verified": False, "terminal": True,
                      "reason": "order contains no matching items"},
            )

        item = items[0]
        return AdapterResult(
            success=True, status_code=resp.status_code, raw=resp.body,
            latency_ms=resp.latency_ms, request_snapshot=resp.request_snapshot,
            data={
                "verified": bool(body.get("verified")),
                "terminal": not body.get("verified"),
                "reason": body.get("verification_note", ""),
                "order_ref": body["order_reference"],
                "order_status": body.get("order_status"),
                "purchased_at": body.get("purchased_at"),
                "buyer_email": body.get("buyer_email"),
                "buyer_name": body.get("buyer_name"),
                "buyer_region": body.get("buyer_region", "IN"),
                "buyer_city": body.get("buyer_city"),
                "model_code": item.get("model_code"),
                "serial_number": item.get("serial_number"),
                "product_name": item.get("product_name"),
                "unit_price": item.get("unit_price", 0.0),
                "seller_code": item.get("seller_code"),
            },
        )


register_adapter(MarketplaceAdapter())
