# ServiceMesh Architecture

## Runtime topology

```text
React customer/provider/admin portals
            |
            v
        FastAPI API
            |
            v
    ServiceMesh Core
    |  Service Transaction
    |  State machine
    |  Policy engine
    |  Provider selection
    |  Compatibility engine
    |  Recovery engine
    |  Notification/event manager
    |
    +--> Marketplace adapter --> Amazon Marketplace Simulator
    +--> OEM adapter         --> Dell OEM Simulator
    +--> Warranty adapter    --> Warranty Provider Simulator
    +--> Service adapter     --> Pune Authorized Service Centre Simulator
    +--> Supplier adapter    --> Parts Supplier Simulator
    |
    +--> PostgreSQL / SQLite
    +--> optional Redpanda
    +--> ML scorer
```

## Service Transaction

The transaction is the cross-company business abstraction. It persists current state, resume state, previous state, state transitions, participants, provider operations, attempts, failures, recovery actions, decisions, events, notifications and final outcome.

It is not an ACID transaction spanning organizations.

## State machine

Happy path:

`CREATED → PURCHASE_VERIFIED → PRODUCT_VERIFIED → WARRANTY_VERIFIED → COVERAGE_CHECKED → PROVIDER_SELECTED → COMPONENT_VALIDATED → PART_REQUESTED → PART_CONFIRMED → REPAIR_SCHEDULED → REPAIR_IN_PROGRESS → REPAIR_COMPLETED → SERVICE_VERIFIED → CLOSED`

Control states include `WAITING`, `RETRYING`, `TIMEOUT`, `ESCALATED`, `COMPENSATING`, `REJECTED`, `FAILED`, and `CANCELLED`.

## Provider adapter boundary

The orchestrator speaks canonical operations such as `VERIFY_PURCHASE`, `CHECK_COVERAGE`, `BOOK_SERVICE`, `RESERVE_PART`, `DISPATCH_PART` and `CONFIRM_COMPLETION`. Adapters translate them into organization-specific endpoints, authentication and payload formats.

## Failure/recovery

- transient provider failure → retry
- timeout on mutation → unknown outcome → reconcile by idempotency key
- provider business rejection → exclude provider and select fallback where possible
- unavailable component → alternative compatible component
- unavailable supplier → alternative supplier
- expired warranty → business rejection, not retry
- cancellation → compensating booking cancellation and part release where applicable

## Persistence boundary

ServiceMesh stores orchestration facts and compatibility catalogue data. Marketplace, warranty and supplier operational records remain in their own simulator databases. This preserves the federation boundary.

## Technology responsibility map

| Technology | Responsibility |
|---|---|
| React/Vite | Product UI |
| FastAPI | API boundary |
| SQLAlchemy + PostgreSQL/SQLite | durable business state |
| Redis | future cache/idempotency/coordination extension |
| Redpanda/Kafka | optional asynchronous event transport |
| ML/scikit-learn | provider risk assistance |
| LLM/GenAI | optional issue-language interpretation |
| Docker | reproducible deployment |

Temporal, Neo4j, OPA, Keycloak, Prometheus and Grafana are intentionally not shipped as dead infrastructure in this MVP. Their integration boundaries and rationale are documented in `RESEARCH.md` and `DEPLOYMENT.md`.
