"""Adapter for the Warranty Provider organization.

Source contract : SOAP-flavoured `Envelope/Header/Body/<Name>Result` JSON,
                  two-header client credentials, domain status codes (WTY-xxx).
Target contract : ServiceMesh VERIFY_WARRANTY and CHECK_COVERAGE.

This organization answers "no" in several distinct ways (no contract, expired,
voided, excluded) and every one of them is a *permanent business rejection*
carried in a 200 OK. Treating any of them as a retryable failure would produce
repeated pointless calls and delay telling the customer the truth.
"""

from __future__ import annotations

from typing import Any, ClassVar

from servicemesh.adapters.base import (
    AdapterResult,
    OperationRequest,
    ProviderAdapter,
    register_adapter,
)
from servicemesh.core.config import get_settings
from servicemesh.core.enums import OperationType


def _unwrap(body: Any, result_name: str) -> tuple[dict | None, str | None]:
    """Pull `<result_name>` out of the SOAP-ish envelope, with its status code."""
    if not isinstance(body, dict):
        return None, None
    env = body.get("Envelope")
    if not isinstance(env, dict):
        return None, None
    code = (env.get("Header") or {}).get("StatusCode")
    payload = (env.get("Body") or {}).get(result_name)
    return (payload if isinstance(payload, dict) else None), code


class WarrantyAdapter(ProviderAdapter):
    adapter_key: ClassVar[str] = "warranty_soap_v1"
    supported_operations: ClassVar[frozenset[OperationType]] = frozenset(
        {OperationType.VERIFY_WARRANTY, OperationType.CHECK_COVERAGE}
    )

    def _auth(self) -> dict:
        s = get_settings()
        return {"X-Client-Id": s.warranty_client_id, "X-Client-Secret": s.warranty_client_secret}

    async def execute(self, request: OperationRequest) -> AdapterResult:
        if request.operation is OperationType.VERIFY_WARRANTY:
            return await self._verify(request)
        if request.operation is OperationType.CHECK_COVERAGE:
            return await self._coverage(request)
        return self._unsupported(request)

    async def _verify(self, request: OperationRequest) -> AdapterResult:
        resp = await self._call(
            request, "POST", "/warranty/soap/verifyContract",
            json={
                "SerialNumber": request.payload["serial_number"],
                "PurchaseDate": request.payload.get("purchased_at"),
            },
            headers=self._auth(),
        )
        if not resp.ok:
            return AdapterResult.from_transport_failure(resp)

        payload, code = _unwrap(resp.body, "VerifyContractResult")
        if payload is None or "Valid" not in payload:
            return AdapterResult.invalid(resp, "warranty verify envelope malformed")

        valid = bool(payload["Valid"])
        return AdapterResult(
            success=True, status_code=resp.status_code, raw=resp.body,
            latency_ms=resp.latency_ms, request_snapshot=resp.request_snapshot,
            data={
                "valid": valid,
                "terminal": bool(payload.get("Terminal", not valid)),
                "reason": payload.get("Reason", ""),
                "status_code": code,
                "contract_no": payload.get("ContractNo"),
                "serial_number": payload.get("SerialNumber"),
                "plan_code": payload.get("PlanCode"),
                "starts_on": payload.get("StartsOn"),
                "expires_on": payload.get("ExpiresOn"),
                "region": payload.get("Region", "IN"),
                "claims_used": payload.get("ClaimsUsed"),
                "max_claims": payload.get("MaxClaims"),
            },
        )

    async def _coverage(self, request: OperationRequest) -> AdapterResult:
        resp = await self._call(
            request, "POST", "/warranty/soap/checkCoverage",
            json={
                "ContractNo": request.payload["contract_no"],
                "ComponentType": request.payload["component_type"],
                "IssueType": request.payload["issue_type"],
                "EstimatedCost": request.payload.get("estimated_cost", 0.0),
                "Region": request.payload.get("region", "IN"),
            },
            headers=self._auth(),
        )
        if not resp.ok:
            return AdapterResult.from_transport_failure(resp)

        payload, code = _unwrap(resp.body, "CheckCoverageResult")
        if payload is None or "Covered" not in payload:
            return AdapterResult.invalid(resp, "warranty coverage envelope malformed")

        covered = bool(payload["Covered"])
        return AdapterResult(
            success=True, status_code=resp.status_code, raw=resp.body,
            latency_ms=resp.latency_ms, request_snapshot=resp.request_snapshot,
            data={
                "covered": covered,
                "terminal": bool(payload.get("Terminal", not covered)),
                "reason": payload.get("Reason", ""),
                "status_code": code,
                "contract_no": payload.get("ContractNo"),
                "approved_amount": payload.get("ApprovedAmount", 0.0),
                "deductible": payload.get("Deductible", 0.0),
                "coverage_limit": payload.get("CoverageLimit", 0.0),
                "authorization_code": payload.get("AuthorizationCode"),
            },
        )


register_adapter(WarrantyAdapter())
