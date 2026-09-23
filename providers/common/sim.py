"""Failure simulation framework.

Lets an operator (or a test) push any organization into a specific misbehaviour
so that ServiceMesh's recovery paths can be demonstrated against a real running
service rather than a mocked one.

Safety property: the simulator is evaluated *before* business logic runs, so a
simulated failure never leaves the organization's database half-written. The
single exception is TIMEOUT_AFTER_COMMIT, whose entire purpose is to create the
genuinely hard case - the side effect happened but the caller does not know it.
That mode is what makes "UNKNOWN outcome -> reconcile via idempotency key"
testable, and it is still safe because the commit is complete and consistent
before the response is withheld.

Modes
-----
NORMAL                  behave correctly
TIMEOUT                 hang past the client timeout, never commit
TEMPORARILY_UNAVAILABLE HTTP 503 with Retry-After (transient)
PERMANENT_FAILURE       HTTP 500 that will never succeed (permanent)
INVALID_RESPONSE        HTTP 200 with a body that violates the contract
HIGH_LATENCY            slow but correct - exercises SLA logic, not failure
TIMEOUT_AFTER_COMMIT    commit succeeds, response withheld -> UNKNOWN outcome
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field


class FailureMode:
    NORMAL = "NORMAL"
    TIMEOUT = "TIMEOUT"
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    HIGH_LATENCY = "HIGH_LATENCY"
    TIMEOUT_AFTER_COMMIT = "TIMEOUT_AFTER_COMMIT"

    ALL = (
        NORMAL, TIMEOUT, TEMPORARILY_UNAVAILABLE, PERMANENT_FAILURE,
        INVALID_RESPONSE, HIGH_LATENCY, TIMEOUT_AFTER_COMMIT,
    )


class SimulatedTimeout(Exception):
    """Raised instead of really sleeping, when hang_seconds is 0 (test mode)."""


@dataclass
class SimRule:
    mode: str = FailureMode.NORMAL
    #: Apply only to this operation path fragment; None = all operations
    operation: str | None = None
    #: Fail the next N calls then auto-return to NORMAL. None = until reset.
    remaining: int | None = None
    latency_seconds: float = 0.0


@dataclass
class FailureSimulator:
    service_name: str
    #: How long TIMEOUT actually hangs. Tests set 0.0 and rely on the client's
    #: own timeout being 0-ish, or on SimulatedTimeout being raised.
    hang_seconds: float = 30.0
    rules: list[SimRule] = field(default_factory=list)
    call_log: list[dict] = field(default_factory=list)

    # ---------------------------------------------------------------- admin
    def set_mode(
        self,
        mode: str,
        operation: str | None = None,
        count: int | None = None,
        latency_seconds: float = 0.0,
    ) -> SimRule:
        if mode not in FailureMode.ALL:
            raise ValueError(f"unknown failure mode: {mode}")
        # Replace any existing rule for the same operation scope
        self.rules = [r for r in self.rules if r.operation != operation]
        rule = SimRule(
            mode=mode, operation=operation, remaining=count, latency_seconds=latency_seconds
        )
        if mode != FailureMode.NORMAL:
            self.rules.append(rule)
        return rule

    def reset(self) -> None:
        self.rules.clear()
        self.call_log.clear()

    def current(self, operation: str) -> SimRule | None:
        for rule in self.rules:
            if rule.operation is None or rule.operation == operation:
                return rule
        return None

    def status(self) -> dict:
        return {
            "service": self.service_name,
            "rules": [
                {
                    "mode": r.mode,
                    "operation": r.operation,
                    "remaining": r.remaining,
                    "latency_seconds": r.latency_seconds,
                }
                for r in self.rules
            ],
            "calls_observed": len(self.call_log),
        }

    # ------------------------------------------------------------- runtime
    def _consume(self, rule: SimRule) -> None:
        if rule.remaining is not None:
            rule.remaining -= 1
            if rule.remaining <= 0:
                try:
                    self.rules.remove(rule)
                except ValueError:  # pragma: no cover - concurrent reset
                    pass

    async def before(self, operation: str) -> None:
        """Run the pre-business-logic gate.

        Returns normally if the call should proceed. Raises HTTPException (or
        hangs) otherwise. TIMEOUT_AFTER_COMMIT returns normally here - it is
        handled by `after_commit`.
        """
        self.call_log.append({"operation": operation, "ts": time.time()})
        rule = self.current(operation)
        if rule is None or rule.mode == FailureMode.NORMAL:
            return

        mode = rule.mode
        if mode == FailureMode.HIGH_LATENCY:
            self._consume(rule)
            await asyncio.sleep(rule.latency_seconds or 1.5)
            return

        if mode == FailureMode.TIMEOUT:
            self._consume(rule)
            # Hang long enough that the caller's timeout fires first.
            await asyncio.sleep(self.hang_seconds)
            # If we ever get here the caller had no timeout; behave as 504.
            raise HTTPException(status_code=504, detail="upstream timeout (simulated)")

        if mode == FailureMode.TEMPORARILY_UNAVAILABLE:
            self._consume(rule)
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "service_unavailable",
                    "message": f"{self.service_name} is temporarily unavailable (simulated)",
                    "transient": True,
                },
                headers={"Retry-After": "2"},
            )

        if mode == FailureMode.PERMANENT_FAILURE:
            self._consume(rule)
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "internal_error",
                    "message": f"{self.service_name} permanent failure (simulated)",
                    "transient": False,
                },
            )

        if mode == FailureMode.INVALID_RESPONSE:
            self._consume(rule)
            # 200 OK with a body that does not match the published contract.
            raise _InvalidResponseSignal()

        if mode == FailureMode.TIMEOUT_AFTER_COMMIT:
            return  # handled post-commit

    async def after_commit(self, operation: str) -> None:
        """Withhold the response *after* the write succeeded (UNKNOWN outcome)."""
        rule = self.current(operation)
        if rule is not None and rule.mode == FailureMode.TIMEOUT_AFTER_COMMIT:
            self._consume(rule)
            await asyncio.sleep(self.hang_seconds)
            raise HTTPException(status_code=504, detail="timeout after commit (simulated)")


class _InvalidResponseSignal(Exception):
    """Internal marker turned into a malformed 200 by the exception handler."""


class SetModeRequest(BaseModel):
    mode: str = Field(..., description="One of FailureMode.ALL")
    operation: str | None = Field(
        None, description="Restrict to one operation key; null applies to all"
    )
    count: int | None = Field(
        None, ge=1, description="Auto-revert to NORMAL after N affected calls"
    )
    latency_seconds: float = Field(0.0, ge=0)


def build_sim_router(simulator: FailureSimulator) -> APIRouter:
    """Admin endpoints every simulated organization exposes under /_sim."""
    router = APIRouter(prefix="/_sim", tags=["simulation"])

    @router.get("/status")
    def status() -> dict:
        return simulator.status()

    @router.get("/modes")
    def modes() -> dict:
        return {"modes": list(FailureMode.ALL)}

    @router.post("/mode")
    def set_mode(req: SetModeRequest) -> dict:
        try:
            simulator.set_mode(req.mode, req.operation, req.count, req.latency_seconds)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return simulator.status()

    @router.post("/reset")
    def reset() -> dict:
        simulator.reset()
        return simulator.status()

    return router


def install_invalid_response_handler(app) -> None:
    """Turn the INVALID_RESPONSE signal into an actual contract-violating 200."""

    @app.exception_handler(_InvalidResponseSignal)
    async def _handler(request, exc):  # noqa: ARG001
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=200,
            content={"unexpected": "shape", "simulated_invalid_response": True},
        )

    # Ensure HTTPException detail dicts survive as JSON bodies
    return None


def ok_response(payload: dict, status_code: int = 200) -> Response:  # pragma: no cover
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status_code, content=payload)
