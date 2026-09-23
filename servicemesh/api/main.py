"""ServiceMesh Core API application."""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from servicemesh.api.routers import (
    admin_router,
    auth_router,
    provider_router,
    txn_router,
)
from servicemesh.core.config import get_settings
from servicemesh.core.logging import configure_logging, correlation_var
from servicemesh.events.bus import register_default_consumers

logger = logging.getLogger("servicemesh.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()

    from servicemesh.adapters.base import load_builtin_adapters, registered_keys
    from servicemesh.core import models  # noqa: F401  (register mappings)
    from servicemesh.core.db import Base, get_engine

    Base.metadata.create_all(get_engine())
    load_builtin_adapters()
    register_default_consumers()

    logger.info(
        "servicemesh started env=%s transport=%s adapters=%s events=%s",
        settings.environment, settings.provider_transport,
        registered_keys(), settings.event_backend,
    )
    yield
    logger.info("servicemesh shutting down")


app = FastAPI(
    title="ServiceMesh Core API",
    version="0.1.0",
    description=(
        "Federated Service Transaction orchestration for cross-company "
        "after-sales coordination."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000",
                   "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def correlation_middleware(request: Request, call_next):
    """Attach a correlation id to every request and every log line it emits."""
    correlation_id = request.headers.get("X-Correlation-Id") or str(uuid.uuid4())
    token = correlation_var.set(correlation_id)
    start = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        correlation_var.reset(token)
    duration_ms = (time.perf_counter() - start) * 1000
    response.headers["X-Correlation-Id"] = correlation_id
    response.headers["X-Response-Time-Ms"] = f"{duration_ms:.1f}"
    logger.info(
        "%s %s -> %s in %.1fms", request.method, request.url.path,
        response.status_code, duration_ms,
    )
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "message": "an unexpected error occurred",
            "correlation_id": correlation_var.get(""),
        },
    )


app.include_router(auth_router)
app.include_router(txn_router)
app.include_router(provider_router)
app.include_router(admin_router)


@app.get("/health", tags=["ops"])
def health() -> dict:
    from sqlalchemy import text

    from servicemesh.core.db import get_engine

    db_ok = True
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        db_ok = False
    settings = get_settings()
    return {
        "service": "servicemesh-core",
        "status": "UP" if db_ok else "DEGRADED",
        "database": "UP" if db_ok else "DOWN",
        "environment": settings.environment,
        "provider_transport": settings.provider_transport,
        "event_backend": settings.event_backend,
    }


@app.get("/", tags=["ops"])
def root() -> dict:
    return {
        "service": "ServiceMesh Core API",
        "docs": "/docs",
        "health": "/health",
    }
