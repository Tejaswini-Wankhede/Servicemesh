"""Test fixtures.

Environment is configured *before* any ServiceMesh module is imported, because
`get_settings()` is lru_cached and the provider stores build their engines
lazily from environment variables on first use.

Each test gets a freshly seeded set of six databases. That is slower than
sharing one, but these tests assert on stock levels, capacity counters and
reservation uniqueness - state that leaks between tests would make failures
non-reproducible, which is the one thing a failure-recovery test suite cannot
afford.
"""

from __future__ import annotations

import os
import tempfile
import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore")

# --- environment must be set before servicemesh imports --------------------
_TMP = Path(tempfile.mkdtemp(prefix="servicemesh-tests-"))
os.environ["ENVIRONMENT"] = "test"
os.environ["PROVIDER_DATA_DIR"] = str(_TMP)
os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{_TMP / 'core.db'}"
os.environ["PROVIDER_TRANSPORT"] = "inproc"
os.environ["PROVIDER_TIMEOUT_SECONDS"] = "0.4"
# Exponential backoff is exercised by asserting on the computed delay, not by
# actually sleeping through it.
os.environ["RETRY_DELAY_MULTIPLIER"] = "0"
os.environ["EVENT_BACKEND"] = "memory"
os.environ["ML_ENABLED"] = "false"
os.environ["GENAI_ENABLED"] = "false"
os.environ["JWT_SECRET"] = "test-secret-for-servicemesh-32-bytes-minimum"

from sqlalchemy import select  # noqa: E402

from providers.manufacturer.app import simulator as manufacturer_sim  # noqa: E402
from providers.manufacturer.app import store as manufacturer_store  # noqa: E402
from providers.marketplace.app import simulator as marketplace_sim  # noqa: E402
from providers.marketplace.app import store as marketplace_store  # noqa: E402
from providers.parts_supplier.app import simulator as supplier_sim  # noqa: E402
from providers.parts_supplier.app import store as supplier_store  # noqa: E402
from providers.service_centre.app import simulator as service_sim  # noqa: E402
from providers.service_centre.app import store as service_store  # noqa: E402
from providers.warranty.app import simulator as warranty_sim  # noqa: E402
from providers.warranty.app import store as warranty_store  # noqa: E402
from scripts.seed import seed_all  # noqa: E402
from servicemesh.core.db import session_scope  # noqa: E402
from servicemesh.core.models import Customer, Provider  # noqa: E402
from servicemesh.events.bus import event_bus, register_default_consumers  # noqa: E402

SIMULATORS = {
    "marketplace": marketplace_sim,
    "manufacturer": manufacturer_sim,
    "warranty": warranty_sim,
    "service_centre": service_sim,
    "parts_supplier": supplier_sim,
}

STORES = [
    marketplace_store, manufacturer_store, warranty_store,
    service_store, supplier_store,
]


@pytest.fixture(autouse=True)
def fresh_environment():
    """Rebuild all six databases and clear all simulator state per test."""
    for sim in SIMULATORS.values():
        sim.reset()
        # Long enough that the client's 0.4s deadline always fires first, but
        # the coroutine is cancelled immediately so no test actually waits.
        sim.hang_seconds = 30.0

    event_bus.clear()
    register_default_consumers()

    seed_all(reset=True)
    yield

    for sim in SIMULATORS.values():
        sim.reset()


@pytest.fixture
def db():
    with session_scope() as session:
        yield session


@pytest.fixture
def simulators():
    return SIMULATORS


@pytest.fixture
def customer(db):
    """Pune customer - nearest authorized centre is SVC-PUNE-01."""
    return db.scalar(select(Customer).where(Customer.external_ref == "CUST-0001"))


@pytest.fixture
def nagpur_customer(db):
    """Nagpur customer - nearest centre is the UNAUTHORIZED SVC-NAG-GREY."""
    return db.scalar(select(Customer).where(Customer.external_ref == "CUST-0002"))


@pytest.fixture
def providers(db):
    return {p.code: p for p in db.scalars(select(Provider))}


def make_request(**overrides) -> dict:
    """A valid happy-path service request; override fields per scenario."""
    payload = {
        "order_ref": "ORD-100001",
        "serial_number": "SN-AX14-0001",
        "issue_type": "BATTERY_FAILURE",
        "issue_description": "Battery swells and the laptop shuts down after 20 minutes",
    }
    payload.update(overrides)
    return payload
