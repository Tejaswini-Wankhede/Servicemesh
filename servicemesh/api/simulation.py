"""Failure-simulation control that works in both deployment topologies.

Two topologies, one API
-----------------------
* `PROVIDER_TRANSPORT=inproc` - the organizations run inside this process
  (tests, single-process local runs). Their `FailureSimulator` objects are
  directly reachable, so we call them.
* `PROVIDER_TRANSPORT=http` - each organization is its own process or
  container (docker-compose, any real deployment). Its simulator lives in
  *that* process and mutating a local object here would do nothing. So we call
  the organization's own `/_sim` admin API over HTTP.

The second case is the one that matters and the one that was originally
missing: setting a failure mode appeared to succeed while silently having no
effect, which is worse than an error because a demonstration would look fine
until the expected failure never arrived. The controller now reports which
channel it used so that a no-op is impossible to mistake for success.
"""

from __future__ import annotations

import logging

import httpx

from servicemesh.core.config import get_settings

logger = logging.getLogger("servicemesh.simulation")

SERVICE_NAMES = (
    "marketplace", "manufacturer", "warranty", "service_centre", "parts_supplier",
)


class SimulationError(Exception):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _base_urls() -> dict[str, str]:
    s = get_settings()
    return {
        "marketplace": s.marketplace_url,
        "manufacturer": s.manufacturer_url,
        "warranty": s.warranty_url,
        "service_centre": s.service_centre_url,
        "parts_supplier": s.parts_supplier_url,
    }


def _in_process_simulators() -> dict:
    from providers.manufacturer.app import simulator as manufacturer_sim
    from providers.marketplace.app import simulator as marketplace_sim
    from providers.parts_supplier.app import simulator as supplier_sim
    from providers.service_centre.app import simulator as service_sim
    from providers.warranty.app import simulator as warranty_sim

    return {
        "marketplace": marketplace_sim,
        "manufacturer": manufacturer_sim,
        "warranty": warranty_sim,
        "service_centre": service_sim,
        "parts_supplier": supplier_sim,
    }


def _is_http() -> bool:
    return get_settings().provider_transport == "http"


def _request(service: str, method: str, path: str, json: dict | None = None) -> dict:
    url = _base_urls().get(service)
    if url is None:
        raise SimulationError(f"unknown service {service}", status_code=404)
    try:
        with httpx.Client(base_url=url, timeout=8.0) as client:
            response = client.request(method, path, json=json)
    except httpx.HTTPError as exc:
        raise SimulationError(
            f"could not reach {service} at {url}: {exc}"
        ) from exc
    if response.status_code >= 400:
        raise SimulationError(
            f"{service} rejected the simulation request "
            f"(HTTP {response.status_code}): {response.text[:300]}",
            status_code=response.status_code if response.status_code < 500 else 502,
        )
    return response.json()


def status() -> dict:
    """Current simulation state of every organization."""
    channel = "http" if _is_http() else "inproc"
    services: dict[str, dict] = {}

    if _is_http():
        for name in SERVICE_NAMES:
            try:
                services[name] = _request(name, "GET", "/_sim/status")
            except SimulationError as exc:
                services[name] = {"service": name, "error": exc.message,
                                  "reachable": False}
    else:
        for name, sim in _in_process_simulators().items():
            services[name] = sim.status()

    return {"channel": channel, "services": services}


def set_mode(
    service: str,
    mode: str,
    operation: str | None = None,
    count: int | None = None,
    latency_seconds: float = 0.0,
) -> dict:
    if service not in SERVICE_NAMES:
        raise SimulationError(f"unknown service {service}", status_code=404)

    if _is_http():
        result = _request(service, "POST", "/_sim/mode", json={
            "mode": mode, "operation": operation, "count": count,
            "latency_seconds": latency_seconds,
        })
        logger.info("simulation set on %s via http: mode=%s op=%s", service, mode, operation)
        return {"channel": "http", **result}

    sim = _in_process_simulators()[service]
    try:
        sim.set_mode(mode, operation, count, latency_seconds)
    except ValueError as exc:
        raise SimulationError(str(exc), status_code=400) from exc
    logger.info("simulation set on %s in-process: mode=%s op=%s", service, mode, operation)
    return {"channel": "inproc", **sim.status()}


def reset(service: str | None = None) -> dict:
    targets = [service] if service else list(SERVICE_NAMES)
    if _is_http():
        for name in targets:
            _request(name, "POST", "/_sim/reset")
    else:
        sims = _in_process_simulators()
        for name in targets:
            sims[name].reset()
    return status()


def modes() -> list[str]:
    from providers.common.sim import FailureMode

    return list(FailureMode.ALL)
