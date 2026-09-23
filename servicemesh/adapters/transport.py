"""Transport layer between ServiceMesh and the external organizations.

Two backends, one interface:

  http    real network calls (docker-compose, staging, production)
  inproc  httpx ASGITransport straight into the provider FastAPI apps

`inproc` exists so the test suite exercises the *real* adapter code, the *real*
provider routing, validation, auth and business logic - just without binding
sockets. It is not a mock: every request still goes through FastAPI's full
request pipeline and hits the organization's real (SQLite) database. Swapping
to `http` changes one environment variable and nothing else.

Timeouts
--------
`asyncio.wait_for` enforces the deadline for both backends. This matters:
ASGITransport has no socket, so httpx's own timeout would never fire against a
provider that is deliberately hanging. Cancelling the coroutine also models
reality accurately - the caller stops waiting, but whatever the provider
already committed stays committed. That is exactly the UNKNOWN-outcome case
the recovery engine has to reason about.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from servicemesh.core.config import get_settings
from servicemesh.core.enums import FailureReason, FailureType


@dataclass
class TransportResponse:
    ok: bool
    status_code: int | None
    body: Any
    latency_ms: float
    #: Populated only when ok is False
    failure_reason: FailureReason | None = None
    failure_type: FailureType | None = None
    error_message: str | None = None
    request_snapshot: dict = field(default_factory=dict)

    def json_body(self) -> dict:
        return self.body if isinstance(self.body, dict) else {}


class ProviderTransport:
    """Issues HTTP requests to an organization and classifies the outcome."""

    def __init__(self) -> None:
        self._asgi_apps: dict[str, Any] | None = None

    # ---------------------------------------------------------------- setup
    def _app_registry(self) -> dict[str, Any]:
        """Lazily import provider apps (only needed for the inproc backend)."""
        if self._asgi_apps is None:
            settings = get_settings()
            from providers.manufacturer.app import app as manufacturer_app
            from providers.marketplace.app import app as marketplace_app
            from providers.parts_supplier.app import app as supplier_app
            from providers.service_centre.app import app as service_app
            from providers.warranty.app import app as warranty_app

            self._asgi_apps = {
                settings.marketplace_url: marketplace_app,
                settings.manufacturer_url: manufacturer_app,
                settings.warranty_url: warranty_app,
                settings.service_centre_url: service_app,
                settings.parts_supplier_url: supplier_app,
            }
        return self._asgi_apps

    def reset(self) -> None:
        self._asgi_apps = None

    def _client(self, base_url: str) -> httpx.AsyncClient:
        settings = get_settings()
        if settings.provider_transport == "inproc":
            app = self._app_registry().get(base_url)
            if app is None:
                raise RuntimeError(
                    f"no in-process app registered for base_url {base_url!r}; "
                    "check provider URLs in settings"
                )
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=base_url
            )
        return httpx.AsyncClient(base_url=base_url, timeout=settings.provider_timeout_seconds)

    # -------------------------------------------------------------- request
    async def request(
        self,
        base_url: str,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        headers: dict | None = None,
        timeout: float | None = None,
    ) -> TransportResponse:
        settings = get_settings()
        deadline = timeout if timeout is not None else settings.provider_timeout_seconds
        snapshot = {
            "method": method, "url": f"{base_url}{path}",
            "params": _redact(params or {}), "json": _redact(json or {}),
            "headers": _redact_headers(headers or {}),
        }
        start = time.perf_counter()

        try:
            async with self._client(base_url) as client:
                response = await asyncio.wait_for(
                    client.request(method, path, json=json, params=params, headers=headers),
                    timeout=deadline,
                )
        except (TimeoutError, httpx.TimeoutException):
            return TransportResponse(
                ok=False, status_code=None, body=None,
                latency_ms=(time.perf_counter() - start) * 1000,
                failure_reason=FailureReason.TIMEOUT,
                # Deliberately TRANSIENT here. The orchestrator upgrades this to
                # UNKNOWN for mutating operations, because only it knows whether
                # the call could have had a side effect.
                failure_type=FailureType.TRANSIENT,
                error_message=f"no response within {deadline}s",
                request_snapshot=snapshot,
            )
        except httpx.ConnectError as exc:
            return TransportResponse(
                ok=False, status_code=None, body=None,
                latency_ms=(time.perf_counter() - start) * 1000,
                failure_reason=FailureReason.CONNECTION_ERROR,
                failure_type=FailureType.TRANSIENT,
                error_message=str(exc), request_snapshot=snapshot,
            )
        except Exception as exc:  # noqa: BLE001 - transport must never leak
            return TransportResponse(
                ok=False, status_code=None, body=None,
                latency_ms=(time.perf_counter() - start) * 1000,
                failure_reason=FailureReason.INTERNAL_ERROR,
                failure_type=FailureType.TRANSIENT,
                error_message=f"{type(exc).__name__}: {exc}",
                request_snapshot=snapshot,
            )

        latency_ms = (time.perf_counter() - start) * 1000
        try:
            body = response.json()
        except ValueError:
            body = response.text

        if response.status_code < 400:
            return TransportResponse(
                ok=True, status_code=response.status_code, body=body,
                latency_ms=latency_ms, request_snapshot=snapshot,
            )

        reason, ftype = _classify_http_error(response.status_code, body)
        return TransportResponse(
            ok=False, status_code=response.status_code, body=body,
            latency_ms=latency_ms, failure_reason=reason, failure_type=ftype,
            error_message=_extract_message(body) or f"HTTP {response.status_code}",
            request_snapshot=snapshot,
        )


def _classify_http_error(status: int, body: Any) -> tuple[FailureReason, FailureType]:
    """Map an HTTP error onto ServiceMesh's failure vocabulary.

    The TRANSIENT/PERMANENT split is the single most consequential
    classification in the system: it decides whether retrying is useful or
    merely wasteful. Getting it wrong in the PERMANENT->TRANSIENT direction
    produces retry storms against an organization that will never say yes.
    """
    if status == 503:
        return FailureReason.SERVICE_UNAVAILABLE, FailureType.TRANSIENT
    if status == 504:
        return FailureReason.TIMEOUT, FailureType.TRANSIENT
    if status == 429:
        return FailureReason.RATE_LIMITED, FailureType.TRANSIENT
    if status >= 500:
        # A provider may mark its own 5xx as transient; honour that hint.
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, dict) and detail.get("transient") is True:
            return FailureReason.SERVICE_UNAVAILABLE, FailureType.TRANSIENT
        return FailureReason.SERVICE_UNAVAILABLE, FailureType.PERMANENT
    if status == 401 or status == 403:
        return FailureReason.UNAUTHORIZED, FailureType.PERMANENT
    if status == 404:
        return FailureReason.NOT_FOUND, FailureType.PERMANENT
    if status == 409:
        # Conflict is the organization saying "no" for a business reason -
        # out of stock, no capacity, serial not in order. Retrying is pointless;
        # ServiceMesh must choose a different provider/component instead.
        return FailureReason.BUSINESS_REJECTION, FailureType.PERMANENT
    if status == 422:
        return FailureReason.INVALID_RESPONSE, FailureType.PERMANENT
    return FailureReason.BUSINESS_REJECTION, FailureType.PERMANENT


def _extract_message(body: Any) -> str | None:
    if not isinstance(body, dict):
        return str(body)[:500] if body else None
    detail = body.get("detail", body)
    if isinstance(detail, dict):
        for key in ("message", "error", "Message", "FaultString", "reason"):
            if key in detail:
                return str(detail[key])
        return str(detail)[:500]
    return str(detail)[:500]


SENSITIVE_KEYS = {"api_key", "password", "secret", "token", "client_secret"}
SENSITIVE_HEADERS = {
    "x-api-key", "authorization", "x-client-secret", "x-partner-key", "x-client-id",
}


def _redact(data: dict) -> dict:
    """Never persist credentials into the audit trail."""
    return {
        k: ("***" if k.lower() in SENSITIVE_KEYS else v) for k, v in data.items()
    }


def _redact_headers(headers: dict) -> dict:
    return {
        k: ("***" if k.lower() in SENSITIVE_HEADERS else v) for k, v in headers.items()
    }


#: Module-level singleton; adapters share one transport.
transport = ProviderTransport()
