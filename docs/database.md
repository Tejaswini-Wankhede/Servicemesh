# Database Documentation

There are **six independent databases** in this system, not one. That
separation is architectural, not incidental — see `docs/architecture.md` §3.1.

1. `servicemesh` (this document) — ServiceMesh Core's own schema: the Service
   Transaction and everything around it, plus the catalogue/compatibility
   data and the provider registry.
2. `org_marketplace`, `org_manufacturer`, `org_warranty`, `org_service_centre`,
   `org_parts_supplier` — one per simulated organization, each with its own
   independent schema (see `providers/*/app.py`). ServiceMesh never queries
   these directly; it only calls their HTTP APIs through the adapter layer.

The table list and columns below were extracted from the live SQLAlchemy
metadata (`Base.metadata.create_all()` against an in-memory database), not
transcribed by hand, so they match the code exactly as of this writing.

---

## Schema initialization

No hand-written SQL migration scripts exist. The schema is created via
`Base.metadata.create_all(engine)`, invoked by:

- `python -m scripts.seed --reset` (drops and recreates before seeding), or
- the API's `lifespan` handler (`servicemesh/api/main.py`), which creates
  tables on startup if they don't already exist.

Alembic is declared as a project dependency (`pyproject.toml`) but no
migration scripts have been written. This is recorded as a known limitation
in the README rather than glossed over — for a schema this size at this
project stage, `create_all` plus `--reset` is adequate, but a production
deployment with real data would need proper migrations before its first
schema change.

---

## ServiceMesh Core schema (19 tables)

### Identity & customers

**`users`** — application accounts (login credentials, role).
`role` ∈ `CUSTOMER | PROVIDER | ADMIN`. A `PROVIDER` user is scoped to exactly
one organization via `provider_id`; a `CUSTOMER` user is scoped to one
`customer_id`. Both are nullable FKs with `ON DELETE SET NULL`.
Password is stored as `hashed_password` (`pbkdf2_sha256`, see
`core/security.py`).

**`customers`** — ServiceMesh's own customer record, keyed by `external_ref`
(the identifier the marketplace would recognise this customer by).

### Catalogue (compatibility domain)

**`product_models`** — a model line (`model_code` unique, e.g.
`AX14-PRO-2023`), with `manufacturer_code` and free-form `attributes` JSON.

**`components`** — a replaceable part SKU (`sku` unique), with
`component_type` (`BATTERY`, `SCREEN`, …), `is_oem_certified`, `revision`, and
`specs` JSON used by the compatibility engine's constraint checks (e.g.
`{"min_bios": "1.5"}` lives in `compatibility_rules.constraints`, not here —
`specs` describes the part itself).

**`compatibility_rules`** — the explicit model↔component compatibility
matrix. `UNIQUE(product_model_id, component_id)`. `verdict` ∈ `COMPATIBLE |
INCOMPATIBLE` (never `UNKNOWN` — an unknown pairing is represented by the
**absence** of a row, per the three-valued logic in
`engines/compatibility.py`). `rule_source` records provenance (e.g.
`OEM_MATRIX`) for auditability.

### Provider registry

**`providers`** — every participating organization ServiceMesh knows about.
`kind` ∈ `MARKETPLACE | MANUFACTURER | WARRANTY | SERVICE_CENTRE |
PARTS_SUPPLIER`. `adapter_key` selects which `ProviderAdapter` class handles
this row (`adapters/base.py::get_adapter()`). Reliability columns
(`total_operations`, `successful_operations`, `failed_operations`,
`total_latency_ms`, `sla_violations`) are **ServiceMesh's own observations**,
updated after every real operation outcome — never a value the provider
reports about itself. `capacity_used` / `capacity_total` drive the hard
capacity constraint in provider selection.

### The Service Transaction and everything around it

**`service_transactions`** — the central table. See
`docs/architecture.md` §1 for the full rationale of every column group:
request fields, evidence JSON columns, decision/selection FKs, external
handles (`part_reservation_ref`, `service_booking_ref` — needed for
compensation), SLA tracking, and outcome bookkeeping. Two FKs to `providers`
(`selected_service_provider_id`, `selected_supplier_id`) plus a FK to
`components` (`selected_component_id`) and to `product_models`.
`excluded_provider_ids` / `excluded_component_ids` are JSON arrays, not join
tables — they represent transient "ruled out during this transaction" state
that recovery reads and (for transient causes) clears, not a permanent
relationship worth normalising.

**`transaction_state_history`** — append-only. Every call to
`StateMachine.transition()` inserts a row here; none is ever updated or
deleted. `from_state` is nullable only for the very first row (transaction
creation has no "from").

**`transaction_participants`** — which organizations are involved in a given
transaction, and in what role. `UNIQUE(transaction_id, provider_id)` — a
provider joins a transaction once, even if it receives many operations.
`joined_state` records what the transaction's state was when this
organization first became involved, useful for the timeline.

**`provider_operations`** — one row per **logical** operation (e.g. "reserve
this battery"), which may span several physical attempts. `UNIQUE
(idempotency_key)` — this is the database-level enforcement of the
idempotency guarantee described in `docs/architecture.md` §6.
`attempt_count` / `max_attempts` drive the recovery engine's retry-budget
check. `is_compensated` / `compensated_by_operation_id` link a mutating
operation to the operation that undid it.

**`operation_attempts`** — one row per **physical** call. `UNIQUE
(operation_id, attempt_number)`. Retries append new rows; nothing here is
ever overwritten, so the full attempt history (including intermediate
timeouts and their latencies) survives for the audit trail and for the
Transaction Detail page's "attempts" drill-down.

**`failure_records`** — every classified failure (`failure_type` ∈
`TRANSIENT|PERMANENT|UNKNOWN`, `failure_reason` ∈ the `FailureReason` enum).
`state_at_failure` captures what the transaction's `state` was at the moment
of failure, independent of where it may have moved since.

**`recovery_actions`** — every decision the recovery engine made:
`strategy`, `rule_id` (traceable back to `engines/recovery.py`'s R1–R8
table), `rationale` (human-readable, shown verbatim on the detail page),
`inputs` (the full `FailureContext` the decision was made from, as JSON —
this is what makes a recovery decision made months ago still explainable),
and `succeeded`.

**`decision_records`** — policy, provider-selection and compatibility
decisions share this one table because they share a shape: `decision_type`,
`verdict`, `subject_label`, `engine`, `rule_version`, `reasons` (list),
`inputs`, and optional `scores` (the full weighted-scoring breakdown for
provider selection, or candidate ranking for compatibility).

**`event_records`** — the persisted half of the transactional-outbox event
bus (`events/bus.py`). `event_id` is unique and is what
`processed_events` keys off for consumer idempotency. `published` records
whether the Kafka publish (if `EVENT_BACKEND=kafka`) succeeded — persistence
here never depends on that.

**`processed_events`** — `UNIQUE(event_id, consumer)`. A consumer claims an
event by inserting here inside a nested transaction (`SAVEPOINT`); an
`IntegrityError` on that insert means the event was already handled, and the
handler is skipped. This is the mechanism, not merely the intent, behind
"consumers are idempotent."

**`audit_events`** — security/operational audit, distinct from domain
events. Every domain event is also mirrored here by the built-in `audit`
consumer (`events/bus.py::audit_consumer`), plus explicit calls from API
routes for actions like `LOGIN`, `CREATE_TRANSACTION`, `SET_SIMULATION`.

**`notifications`** — customer-facing milestone notifications, generated by
`events/bus.py::notification_consumer` for a fixed set of event types
(`TransactionCreated`, `WarrantyVerified`, `ServiceScheduled`,
`RepairCompleted`, `TransactionClosed`, `TransactionRejected`,
`TransactionEscalated`).

**`provider_metric_snapshots`** — periodic rollup of provider behaviour,
intended as the feature source for retraining the ML model against real
(rather than only synthetic) operational history. Not currently populated by
a scheduled job — the table and model exist; the rollup job does not yet.
Recorded here as a genuine gap rather than omitted from the documentation.

---

## Indexing strategy

Every foreign key has a corresponding index (SQLAlchemy's
`ForeignKey(..., index=True)` or explicit `Index(...)` in `__table_args__`).
Composite indexes exist where the query pattern is genuinely composite:

- `ix_txn_state_created` on `(state, created_at)` — dashboard queries filter
  by state and sort by recency.
- `ix_txn_customer_state` on `(customer_id, state)` — a customer's
  transaction list filtered by state.
- `ix_op_txn_seq` on `(transaction_id, sequence)` — operations are always
  read in sequence order for one transaction (the detail page timeline).
- `ix_op_provider_status` on `(provider_id, status)` — the provider portal's
  "my jobs" query.
- `ix_compat_lookup` on `(product_model_id, verdict)` — the compatibility
  engine's `find_compatible()` hot path.
- `ix_event_txn_type` on `(transaction_id, event_type)`.

---

## Referential integrity note

SQLite does not enforce foreign keys by default; `core/db.py` explicitly
issues `PRAGMA foreign_keys=ON` on every new SQLite connection
(`_set_sqlite_pragmas`), so constraint violations are caught in development
and tests exactly as they would be under PostgreSQL. This is what surfaced a
real seeding bug during development: `CompatibilityRule` initially had no
declared ORM `relationship()` to its two parent tables, so SQLAlchemy's
unit-of-work could not infer insert ordering and emitted the child `INSERT`
before its parents existed. The fix was adding the relationships (which also
made the ORM graph nicer to work with) plus explicit `db.flush()` calls
between dependent groups in `scripts/seed.py`.

---

## Organization schemas (owned independently)

These are documented briefly here for completeness; ServiceMesh never queries
them directly.

| Organization | Key tables | Notable columns |
|---|---|---|
| Marketplace | `mkt_customers`, `mkt_orders`, `mkt_order_items` | `order_reference` unique |
| Manufacturer | `oem_models`, `oem_units`, `oem_authorized_partners` | `serial_number` unique on `oem_units`; `approved_component_skus` JSON on `oem_models` |
| Warranty | `wty_contracts` | `contract_no` + `serial_number` both unique; `covered_component_types` / `excluded_issue_types` JSON |
| Service centre | `svc_branches`, `svc_bookings` | `idempotency_key` unique on `svc_bookings` |
| Parts supplier | `sup_stock`, `sup_reservations` | `idempotency_key` unique on `sup_reservations` |

The two `idempotency_key` unique constraints above are the organization-side
half of the idempotency guarantee described in `docs/architecture.md` §6 — the
half that makes a replayed reservation or booking request return the
*original* record rather than create a new one, independent of anything
ServiceMesh remembers.
