# ServiceMesh

ServiceMesh is a federated, stateful and failure-aware orchestration platform for cross-organization consumer-electronics after-sales service.

## Product journey

Customer React UI → FastAPI → ServiceMesh orchestration → Amazon Marketplace Simulator → Dell OEM Simulator → Warranty Provider Simulator → authorized service centre → repair person → compatible part → parts supplier → repair → independent verification → in-app customer notification.

The React UI is the product. `/docs` is a developer API tool only.

## What is actually implemented

- Stateful Service Transaction with persisted state history, resume state and audit trail.
- Real HTTP organization simulators in Docker; in-process ASGI transport for tests.
- Provider adapters that isolate organization-specific API contracts.
- Deterministic warranty/coverage, provider-selection and compatibility decisions.
- Issue-driven component selection for battery, display, keyboard, storage, fan and other issue classes.
- Idempotent mutating operations and reconciliation after timeout-after-commit.
- Failure classification, retry, fallback, compensation and escalation.
- Customer, service-centre and parts-supplier portals backed by real APIs.
- Admin operations dashboard and failure simulation controls.
- Persisted in-app notifications.
- Domain events with optional Redpanda publishing.
- ML-assisted provider risk scoring with deterministic constraints remaining authoritative.
- Deterministic natural-language issue extraction fallback; optional GenAI extension.
- Docker Compose deployment for the core and five simulated organizations.

## Important architecture choice

Temporal is not bundled into the default MVP. ServiceMesh owns the business state machine in PostgreSQL and exposes a resumable workflow boundary. Adding Temporal later is intentionally documented as an execution-layer evolution rather than creating a second source of truth. Kafka/Redpanda is optional event transport and is never the business-state authority.

## Demo accounts

- Customer: `aarti@example.com` / `customer123`
- Admin: `admin@servicemesh.io` / `admin123`
- Service centre: `svc-pune-01@partners.servicemesh.io` / `provider123`
- Parts supplier: `sup-a@partners.servicemesh.io` / `provider123`

These are synthetic demo identities only.

## Local setup

See `SETUP.md`.

## Docker setup

See `DEPLOYMENT.md`.

## Verification

Backend test suites are under `tests/`. Run:

```bash
python -m pytest -q tests/test_api.py
python -m pytest -q tests/test_scenarios.py
python -m pytest -q tests/test_ml.py
```

The frontend requires Node/npm. In the build environment used for this delivery, the npm registry was unavailable, so a fresh `npm ci`/Vite build could not be executed; this is recorded rather than represented as a passing result.
