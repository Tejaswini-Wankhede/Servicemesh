"""Provider Adapter Layer - the anti-corruption boundary.

ServiceMesh's orchestrator speaks exactly one vocabulary: `OperationType`. It
never sees `SerialNo`, `VerifyContractResult`, `X-Partner-Key` or
`{"ok": true, "data": ...}`. Adapters own all of that.

The contract is deliberately narrow:

    execute(request) -> AdapterResult

`AdapterResult.data` is *normalised* - the same field names regardless of which
organization answered. This is what makes it possible to replace a simulated
organization with a real enterprise API by writing one new adapter class and
changing one `adapter_key` value in the provider registry, with zero changes to
the state machine, policy engine or recovery engine.

Distinguishing "failed" from "said no"
--------------------------------------
`AdapterResult.success=False` means the call did not produce a usable answer
(timeout, 503, malformed body) - recovery may retry.
`AdapterResult.success=True` with `data["...verdict..."]=False` means the
organization answered correctly and the answer was negative - a business
decision that retrying cannot change. Conflating these two is the classic
integration bug this layer exists to prevent.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from servicemesh.adapters.transport import TransportResponse, transport
from servicemesh.core.enums import FailureReason, FailureType, OperationType


@dataclass(frozen=True)
class ProviderRef:
    """Everything an adapter needs about a provider, decoupled from the ORM."""

    id: str
    code: str
    kind: str
    base_url: str
    adapter_key: str
    region: str = "IN"
    metadata: dict = field(default_factory=dict)


@dataclass
class OperationRequest:
    operation: OperationType
    provider: ProviderRef
    payload: dict
    correlation_id: str
    idempotency_key: str | None = None
    timeout: float | None = None


@dataclass
class AdapterResult:
    success: bool
    data: dict = field(default_factory=dict)
    raw: Any = None
    status_code: int | None = None
    latency_ms: float = 0.0
    failure_reason: FailureReason | None = None
    failure_type: FailureType | None = None
    error_message: str | None = None
    request_snapshot: dict = field(default_factory=dict)

    @classmethod
    def from_transport_failure(cls, resp: TransportResponse) -> AdapterResult:
        return cls(
            success=False, data={}, raw=resp.body, status_code=resp.status_code,
            latency_ms=resp.latency_ms, failure_reason=resp.failure_reason,
            failure_type=resp.failure_type, error_message=resp.error_message,
            request_snapshot=resp.request_snapshot,
        )

    @classmethod
    def invalid(cls, resp: TransportResponse, detail: str) -> AdapterResult:
        """Provider returned 2xx but the body did not match its contract."""
        return cls(
            success=False, data={}, raw=resp.body, status_code=resp.status_code,
            latency_ms=resp.latency_ms,
            failure_reason=FailureReason.INVALID_RESPONSE,
            failure_type=FailureType.TRANSIENT,
            error_message=detail, request_snapshot=resp.request_snapshot,
        )


class ProviderAdapter(ABC):
    """Base class for all provider adapters."""

    #: Value stored in Provider.adapter_key
    adapter_key: ClassVar[str]
    #: Operations this adapter can service
    supported_operations: ClassVar[frozenset[OperationType]]

    def supports(self, operation: OperationType) -> bool:
        return operation in self.supported_operations

    @abstractmethod
    async def execute(self, request: OperationRequest) -> AdapterResult:
        ...

    # ------------------------------------------------------------- helpers
    async def _call(
        self,
        request: OperationRequest,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> TransportResponse:
        hdrs = dict(headers or {})
        # Correlation id travels to every organization so one transaction can
        # be followed across five separate services' logs.
        hdrs["X-Correlation-Id"] = request.correlation_id
        if request.idempotency_key:
            hdrs.setdefault("Idempotency-Key", request.idempotency_key)
        return await transport.request(
            request.provider.base_url, method, path,
            json=json, params=params, headers=hdrs, timeout=request.timeout,
        )

    @staticmethod
    def _unsupported(request: OperationRequest) -> AdapterResult:
        return AdapterResult(
            success=False,
            failure_reason=FailureReason.INTERNAL_ERROR,
            failure_type=FailureType.PERMANENT,
            error_message=(
                f"adapter does not support operation {request.operation}"
            ),
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, ProviderAdapter] = {}


def register_adapter(adapter: ProviderAdapter) -> None:
    _REGISTRY[adapter.adapter_key] = adapter


def get_adapter(adapter_key: str) -> ProviderAdapter:
    try:
        return _REGISTRY[adapter_key]
    except KeyError as exc:
        raise KeyError(
            f"no adapter registered for key {adapter_key!r}; "
            f"known keys: {sorted(_REGISTRY)}"
        ) from exc


def registered_keys() -> list[str]:
    return sorted(_REGISTRY)


def load_builtin_adapters() -> None:
    """Import and register the adapters for the five simulated organizations."""
    from servicemesh.adapters import (  # noqa: F401
        manufacturer,
        marketplace,
        parts_supplier,
        service_centre,
        warranty,
    )
