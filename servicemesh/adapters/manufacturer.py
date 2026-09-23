"""Adapter for the Manufacturer/OEM organization.

Source contract : `{"Status": "...", "Code": "...", "Data": {PascalCase}}`,
                  Bearer token auth.
Target contract : ServiceMesh VERIFY_PRODUCT and CHECK_PROVIDER_AUTHORIZATION.
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


class ManufacturerAdapter(ProviderAdapter):
    adapter_key: ClassVar[str] = "manufacturer_v2"
    supported_operations: ClassVar[frozenset[OperationType]] = frozenset(
        {OperationType.VERIFY_PRODUCT, OperationType.CHECK_PROVIDER_AUTHORIZATION}
    )

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {get_settings().manufacturer_token}"}

    async def execute(self, request: OperationRequest) -> AdapterResult:
        if request.operation is OperationType.VERIFY_PRODUCT:
            return await self._verify_product(request)
        if request.operation is OperationType.CHECK_PROVIDER_AUTHORIZATION:
            return await self._check_authorization(request)
        return self._unsupported(request)

    async def _verify_product(self, request: OperationRequest) -> AdapterResult:
        serial = request.payload["serial_number"]
        resp = await self._call(
            request, "GET", f"/oem/v2/registry/{serial}", headers=self._auth()
        )

        if not resp.ok:
            if resp.status_code == 404:
                # Serial genuinely not in the OEM registry. Permanent.
                return AdapterResult(
                    success=True, status_code=404, raw=resp.body,
                    latency_ms=resp.latency_ms, request_snapshot=resp.request_snapshot,
                    data={
                        "verified": False, "terminal": True,
                        "reason": f"serial {serial} not found in OEM registry",
                    },
                )
            return AdapterResult.from_transport_failure(resp)

        body = resp.json_body()
        data = body.get("Data")
        if body.get("Status") != "SUCCESS" or not isinstance(data, dict):
            return AdapterResult.invalid(
                resp, "OEM response missing Status=SUCCESS / Data envelope"
            )

        # PascalCase -> ServiceMesh vocabulary. The orchestrator never sees
        # 'SerialNo' or 'ModelCode'.
        return AdapterResult(
            success=True, status_code=resp.status_code, raw=resp.body,
            latency_ms=resp.latency_ms, request_snapshot=resp.request_snapshot,
            data={
                "verified": True,
                "terminal": False,
                "reason": "serial verified against OEM registry",
                "serial_number": data.get("SerialNo"),
                "model_code": data.get("ModelCode"),
                "model_name": data.get("ModelName"),
                "manufacturer_code": data.get("ManufacturerCode"),
                "category": data.get("Category", "LAPTOP"),
                "manufactured_on": data.get("ManufacturedOn"),
                "bios_version": data.get("BiosVersion"),
                "is_recalled": bool(data.get("IsRecalled")),
                "standard_warranty_months": data.get("StandardWarrantyMonths"),
                "approved_component_skus": data.get("ApprovedComponentSkus", []),
            },
        )

    async def _check_authorization(self, request: OperationRequest) -> AdapterResult:
        resp = await self._call(
            request, "POST", "/oem/v2/partners/verify",
            json={
                "PartnerCode": request.payload["partner_code"],
                "Category": request.payload.get("category", "LAPTOP"),
                "Region": request.payload.get("region", "IN"),
            },
            headers=self._auth(),
        )
        if not resp.ok:
            return AdapterResult.from_transport_failure(resp)

        body = resp.json_body()
        data = body.get("Data")
        if not isinstance(data, dict) or "Authorized" not in data:
            return AdapterResult.invalid(resp, "OEM authorization response malformed")

        authorized = bool(data["Authorized"])
        return AdapterResult(
            success=True, status_code=resp.status_code, raw=resp.body,
            latency_ms=resp.latency_ms, request_snapshot=resp.request_snapshot,
            data={
                "authorized": authorized,
                # Not authorized is a valid answer, not a failure - but it is
                # terminal for *this provider*, so the orchestrator must pick a
                # different one rather than retry.
                "terminal": not authorized,
                "reason": data.get("Reason", ""),
                "partner_code": data.get("PartnerCode"),
                "partner_name": data.get("PartnerName"),
                "authorization_level": data.get("AuthorizationLevel"),
            },
        )


register_adapter(ManufacturerAdapter())
