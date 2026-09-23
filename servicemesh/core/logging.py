"""Structured logging with correlation-id propagation.

Every log line carries the correlation id of the Service Transaction (or HTTP
request) that produced it. That is what makes it possible to follow one
customer's problem across ServiceMesh and five separate organizations - the
same id is also sent outbound in the `X-Correlation-Id` header by every
adapter, so the provider services log it too.

JSON output is used outside local development so the lines are machine-parsable
by whatever log stack the deployment uses.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar

correlation_var: ContextVar[str] = ContextVar("correlation_id", default="")
transaction_var: ContextVar[str] = ContextVar("transaction_ref", default="")


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_var.get("")
        record.transaction_ref = transaction_var.get("")
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", ""),
            "transaction_ref": getattr(record, "transaction_ref", ""),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


class HumanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        cid = getattr(record, "correlation_id", "")
        suffix = f" [cid={cid[:8]}]" if cid else ""
        return (
            f"{self.formatTime(record, '%H:%M:%S')} "
            f"{record.levelname:<7} {record.name:<28} "
            f"{record.getMessage()}{suffix}"
        )


def configure_logging(level: int | None = None) -> None:
    from servicemesh.core.config import get_settings

    settings = get_settings()
    resolved = level if level is not None else (
        logging.DEBUG if settings.debug else logging.INFO
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(ContextFilter())
    handler.setFormatter(
        HumanFormatter() if settings.environment in {"local", "test"} else JsonFormatter()
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(resolved)

    # These are noisy and rarely useful at INFO during normal operation.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def set_transaction_context(correlation_id: str, reference: str = "") -> None:
    correlation_var.set(correlation_id)
    if reference:
        transaction_var.set(reference)
