# Architecture

This document describes how ServiceMesh is put together and, more importantly,
*why* each piece exists. It assumes you have read the README's problem
statement.

---

## 1. The core abstraction: the Service Transaction

Everything in this system exists to serve one object: the **Service
Transaction** (`ServiceTransaction` in `servicemesh/core/models.py`). It is the
row that represents "this customer's after-sales request, wherever it
currently stands across five organizations."

It is explicitly **not** a database transaction. No commit can span the
marketplace, the OEM, the warranty provider, the service centre and the
supplier, because they are five separate databases owned by five separate
parties. What the Service Transaction provides instead:

- a persisted **state machine** (`state`, `resume_state`, `previous_state`)
- an append-only **state history** (`TransactionStateHistory`) — never updated,
  never deleted
- every **operation** issued to every organization, with every **attempt**
  (`ProviderOperation`, `OperationAttempt`)
- every **failure** classified by type and reason (`FailureRecord`)
- every **recovery decision** with its rationale and rule id (`RecoveryAction`)
- every **policy, compatibility and selection decision** with reasons and
  score breakdowns (`DecisionRecord`)
- every **domain event** (`EventRecord`)
- SLA due date, breach flag, manual-intervention count, retry count, outcome

If you delete every other design decision in this document, keep this one:
**the transaction's row is the single source of truth for "where are we and
what has already happened", and every other component reads from it or writes
to it rather than keeping its own memory of progress.**

### 1.1 `state` vs `resume_state`

This is the mechanism that makes recovery *resume* rather than *restart*.

- `state` — where the transaction is **right now**. May be a control state
  like `RETRYING`, `TIMEOUT`, `WAITING` or `ESCALATED`.
- `resume_state` — the last **happy-path milestone** that completed safely.

`orchestration/state_machine.py::StateMachine.transition()` updates
`resume_state` only when moving *forward* on the happy path; control states
never move it. When recovery needs to put the transaction back to work
(`Orchestrator._resume_to`), it goes to `resume_state`, never further back.

Concretely: a supplier timeout during `PART_REQUESTED` does not cause
`VERIFY_PURCHASE`, `VERIFY_PRODUCT` or `VERIFY_WARRANTY` to run again. Those
operations already succeeded; their `ProviderOperation` rows are `SUCCEEDED`
and idempotency lookups (`orchestration/idempotency.py`) recognise them as
already done if anything tries to repeat them anyway.

### 1.2 What ServiceMesh does *not* store

Purchase records, warranty contracts and parts inventory live in the
organizations' own databases. ServiceMesh stores a *reference* (order id,
contract number, reservation ref) and the *evidence* each organization
returned at verification time (`purchase_evidence`, `product_evidence`,
`warranty_evidence`, `coverage_evidence` — JSON columns on
`ServiceTransaction`).

The one deliberate exception is the **product/component/compatibility
catalogue** (`ProductModel`, `Component`, `CompatibilityRule`). Compatibility
spans OEM and supplier boundaries — neither organization owns the full graph —
and the compatibility engine must reason about it deterministically and
offline. See §4.3.

---

## 2. Layered structure

```
servicemesh/
├── core/            domain model: config, db, ORM models, enums, security, logging
├── adapters/        transport + one adapter per organization
├── engines/         policy, selection, compatibility, recovery — all deterministic
├── orchestration/   state machine, orchestrator, workflow steps, idempotency
├── events/          persist-then-publish event bus
├── api/             FastAPI routers, schemas, serializers, RBAC
├── ml/              provider-risk model: dataset, train, predict
└── genai/           natural-language issue extraction
```

Dependency direction is strictly downward: `api` depends on `orchestration`,
which depends on `engines` and `adapters`, which depend on `core`. Nothing in
`core`, `engines` or `adapters` imports from `api` or `orchestration`. This is
what lets the engines be unit-tested with a bare database session and no HTTP
server (see `tests/test_scenarios.py` calling `Workflow` directly).

---

## 3. Provider isolation and the adapter layer

### 3.1 Why five separate services

Each organization (`providers/marketplace`, `providers/manufacturer`,
`providers/warranty`, `providers/service_centre`, `providers/parts_supplier`)
is:

- its own FastAPI app,
- with its own SQLAlchemy `Base` and its own database (`providers/common/storage.py`),
- with its own API contract shape (snake_case REST / PascalCase envelope /
  SOAP-ish envelope / `{result,data}` / `{ok,data}`),
- with its own authentication mechanism (API key header / bearer token /
  two-header client credentials / partner key header / API key query param).

This is not decoration. If all five shared one schema, a single SQL
transaction could span them and the entire premise of the project would
disappear. The deliberately different API shapes force a genuine adapter layer
to exist rather than a thin pass-through.

### 3.2 The adapter contract

`servicemesh/adapters/base.py` defines the boundary:

```python
class ProviderAdapter(ABC):
    async def execute(self, request: OperationRequest) -> AdapterResult: ...
```

`OperationRequest` carries a standardized `OperationType` (`VERIFY_PURCHASE`,
`RESERVE_PART`, …) and a plain dict payload. `AdapterResult` carries a
normalized `data` dict with the **same field names regardless of which
organization answered**. The orchestrator never sees `SerialNo` — it sees
`serial_number`, produced by `ManufacturerAdapter` translating the OEM's
PascalCase envelope.

Replacing a simulated organization with a real enterprise API means writing
one new `ProviderAdapter` subclass and changing the `adapter_key` column on
that `Provider` row. Zero changes to the state machine, policy engine, or
recovery engine.

### 3.3 `success` vs the business answer

The single most important distinction in the adapter layer:

```
AdapterResult.success = False   -> no usable answer came back
                                   (timeout, 5xx, malformed body)
                                   -> recovery MAY retry

AdapterResult.success = True    -> the organization answered correctly,
      data["valid"] = False        and the answer was NO
                                   -> a business decision; retrying is pointless
```

An expired warranty returns `success=True, data={"valid": False, "terminal":
True, "reason": "warranty expired on 2026-08-01"}`. The HTTP call succeeded;
the organization's answer did not change. Recovery must never conflate "the
call failed" with "the organization said no" — the entire failure/recovery
matrix (§5) depends on this being computed correctly at the adapter, not
guessed at downstream.

### 3.4 Transport

`servicemesh/adapters/transport.py` provides one `ProviderTransport` with two
backends, selected by `PROVIDER_TRANSPORT`:

- `inproc` — `httpx.ASGITransport` calling the provider FastAPI app directly
  in-process. Used by the test suite. This is a **real execution path**, not a
  mock: full FastAPI routing, Pydantic validation, auth dependencies and the
  organization's actual (SQLite) database all run. It just doesn't bind a
  socket.
- `http` — real network calls. Used by docker-compose and any real deployment.

`asyncio.wait_for()` enforces the timeout for both backends, because
`ASGITransport` has no socket for httpx's own timeout to act on. Cancelling the
coroutine on timeout is also the behaviourally correct model: the caller stops
waiting, but anything the provider already committed stays committed — which
is exactly the UNKNOWN-outcome case the recovery engine has to reason about.

---

## 4. The three deterministic engines

### 4.1 Policy engine (`engines/policy.py`)

A named `rule_set` (e.g. `WARRANTY_ELIGIBILITY`, `PROVIDER_ELIGIBILITY`) is a
list of small pure functions, each taking a context dict and returning a
`RuleOutcome(rule_id, passed, reason, terminal, details)`. `evaluate()` runs
all of them and returns a `PolicyDecision` with the full list plus the
violations.

The `terminal` flag on each outcome is what separates "stop the transaction"
from "try something else": an expired warranty is `terminal=True` (nothing
downstream can fix it); an unauthorized service centre is `terminal=False`
(pick a different centre).

**Why not OPA**: the rules read live evidence gathered mid-transaction from
five organizations and every decision persists the exact inputs. An external
Rego service would add a network hop and a second failure domain to the
critical path for no capability this shape doesn't already provide.
`PolicyEngine.evaluate(rule_set, context) -> Decision` is intentionally the
same shape OPA expects, so swapping the evaluator later is a contained change.

### 4.2 Provider selection engine (`engines/selection.py`)

Two phases, never merged:

1. **Hard constraints** eliminate ineligible providers: wrong region, no
   capacity, not authorized for the manufacturer, SLA beyond the ceiling,
   already excluded this transaction. These are booleans — a provider that
   fails one cannot be redeemed by a good score elsewhere.
2. **Weighted scoring** ranks the survivors on reliability, SLA, distance,
   cost and capacity headroom (`DEFAULT_WEIGHTS`), each normalised into
   `[0, 1]`.

The ML risk score (§7), when available, adjusts the score by at most `0.1` and
is applied **after** phase 1. It can reorder eligible providers; it cannot
resurrect one Phase 1 eliminated. `tests/test_ml.py::test_ml_cannot_rescue_an_ineligible_provider`
asserts this directly.

Every selection persists its full input set, the eliminated providers with
reasons, and the score breakdown of the winner (`DecisionRecord.scores`), so
"why this provider?" is always answerable from the database.

### 4.3 Compatibility engine (`engines/compatibility.py`)

Three-valued logic: `COMPATIBLE`, `INCOMPATIBLE`, `UNKNOWN`. The absence of a
rule is `UNKNOWN`, never `COMPATIBLE` — a missing entry in the OEM's matrix
must never be silently treated as approval.

`CompatibilityRule` rows carry optional `constraints` (e.g. `{"min_bios":
"1.5"}`) that are evaluated against context gathered from the OEM's own serial
lookup (`bios_version`), so a battery revision that requires a newer BIOS
correctly drops out of the candidate list for units that haven't been updated.

`find_compatible()` returns candidates ranked OEM-certified-first, then by
revision, giving a deterministic, reproducible ordering — critical for the
demo scenarios and the test suite to be repeatable.

No LLM is involved in this decision. An incorrect compatibility decision
damages hardware and voids warranties; the GenAI module (§8) may suggest an
issue type from free text, but this engine decides, using explicit rows the
OEM published.

---

## 5. Recovery engine and the failure/recovery matrix

`engines/recovery.py::RecoveryEngine.decide(FailureContext) -> RecoveryDecision`
is a deterministic, ordered rule table (R1–R8), documented in full in the
README. The three commitments it encodes, restated because they are the crux
of the "research contribution" claim:

1. **Never retry a permanent failure** (`FailureType.PERMANENT`). A 404, a
   business rejection, an expired warranty return the same answer forever.
2. **Never guess about an unknown outcome.** A timeout on a *mutating*
   operation (`RESERVE_PART`, `BOOK_SERVICE`) means the side effect may or may
   not have happened. The only correct move is `RECONCILE_VIA_IDEMPOTENCY` —
   ask the organization, keyed by the idempotency key already held
   (`GET_OPERATION_STATUS`).
3. **Deterministic first.** An `ml_advisor` hook exists but may only annotate
   the decision (`RecoveryDecision.ml_advisory`); it cannot invent a strategy
   or authorise retrying a `PERMANENT` failure. `test_rec.py`-style assertions
   (see `tests/test_scenarios.py`) confirm no permanent failure is ever
   assigned a retry strategy.

### 5.1 Failure classification

`servicemesh/adapters/transport.py::_classify_http_error()` maps HTTP status
codes onto `(FailureReason, FailureType)`:

| Status | Reason | Type |
|---|---|---|
| 503 | `SERVICE_UNAVAILABLE` | `TRANSIENT` |
| 504 | `TIMEOUT` | `TRANSIENT` |
| 429 | `RATE_LIMITED` | `TRANSIENT` |
| 5xx (else) | `SERVICE_UNAVAILABLE` | `PERMANENT` (unless body marks `transient: true`) |
| 401/403 | `UNAUTHORIZED` | `PERMANENT` |
| 404 | `NOT_FOUND` | `PERMANENT` |
| 409 | `BUSINESS_REJECTION` | `PERMANENT` |
| 422 | `INVALID_RESPONSE` | `PERMANENT` |

Getting the `TRANSIENT`/`PERMANENT` split wrong in the "everything is
transient" direction produces retry storms against organizations that will
never say yes — this table is the single point of truth for that
classification.

### 5.2 Recovery application

`orchestration/orchestrator.py::Orchestrator.apply_recovery()` takes a
`RecoveryDecision` and executes it: transitions the state machine, optionally
excludes a provider or component, applies backoff delay, invokes
reconciliation, or runs compensation. Every action is recorded as a
`RecoveryAction` row with `strategy`, `rule_id`, `rationale`, and
`succeeded`.

### 5.3 Compensation (Saga pattern)

`Orchestrator.compensate()` undoes committed side effects most-recent-first:
cancel the service booking, then release the part reservation. This runs when
recovery decides `COMPENSATE` (permanent failure with uncompensated side
effects) and on explicit cancellation (`POST /transactions/{id}/cancel`).

---

## 6. Idempotency

`orchestration/idempotency.py::make_idempotency_key()` derives a key as:

```
sha256(transaction_id | operation_type | provider_id | business_scope(payload))
```

`business_scope()` extracts only the fields that make an operation *logically*
distinct (e.g. `{sku, quantity}` for `RESERVE_PART`), so retrying the same
reservation attempt produces the same key, while reserving a *different* part
on the same transaction produces a different one.

**Two layers of protection, both necessary:**

1. ServiceMesh side — `provider_operations.idempotency_key` has a unique
   index. Before issuing a call, `check_idempotency()` looks for an existing
   operation with the same key; if it already `SUCCEEDED`, the recorded
   response is reused and the network is never touched.
2. Organization side — the supplier's and service centre's own databases
   enforce a unique index on the key they receive
   (`SupplierReservation.idempotency_key`, `ServiceBooking.idempotency_key`).
   A replayed request returns the *original* record with `replayed: true`.

Layer 1 alone is insufficient — ServiceMesh could crash after sending but
before recording. Layer 2 alone is insufficient — ServiceMesh would still burn
a network call and have to interpret the replay. Both together make retries
genuinely safe, and `test_scenario_7_repeated_reservation_does_not_double_book`
verifies it against the supplier's actual database, not against ServiceMesh's
belief about what happened.

A subtlety worth recording: **every** operation gets an internal fingerprint
for retry accounting, including read-only ones — without it, each retry of a
read-only check would create a new row with `attempt_count=1` and the retry
budget would never exhaust (this was a real bug found by the test suite; see
`CHANGELOG` notes in the code comments of `idempotency.py`). Only *mutating*
operations transmit the key on the wire (`wire_key`), since sending
`Idempotency-Key` on a GET is meaningless.

---

## 7. Machine learning integration

`servicemesh/ml/predict.py::ProviderRiskScorer` loads a serialized
scikit-learn pipeline (trained by `ml/train.py` on the synthetic dataset from
`ml/dataset.py`) and scores a `Provider` ORM object with a probability of
failure.

The integration boundary is `engines/selection.py::ProviderSelectionEngine.select(..., ml_scorer=...)`:
the scorer is called **after** Phase 1 hard-constraint filtering, and its
contribution to the final score is capped at `0.1`. If the model is missing,
disabled (`ML_ENABLED=false`), or throws, selection proceeds unchanged — ML is
a bounded ranking signal, not a dependency.

---

## 8. GenAI integration

`servicemesh/genai/extract.py::extract_issue()` always runs a rule-based
extractor (regex patterns over symptom keywords) and optionally cross-checks
against an LLM backend (`GENAI_ENABLED`). Agreement between the two raises
confidence; disagreement *lowers* it and flags the request for human review.
Below `REVIEW_THRESHOLD` (0.45), the API returns 422 rather than guessing.

The extracted `issue_type` is written to `nlp_extraction` on the transaction
and is **advisory only**: it feeds into `required_component_type` resolution,
but every downstream decision (warranty, coverage, compatibility,
authorization) still runs through the deterministic engines against the
organizations' own data.

---

## 9. Events

`servicemesh/events/bus.py::EventBus` implements a transactional outbox: every
`DomainEvent` is written to `EventRecord` in the same database transaction as
the state change that produced it, then dispatched to in-process subscribers,
then optionally published to Kafka/Redpanda. Consumers are made idempotent via
`ProcessedEvent`, a `(event_id, consumer)` unique index — a redelivered event
is recorded once and skipped thereafter.

This ordering means the audit trail never depends on a broker being reachable,
and an event can never describe a state change that was later rolled back.

---

## 10. API and RBAC

`servicemesh/api/deps.py` centralises authorization. Three roles: `CUSTOMER`,
`PROVIDER`, `ADMIN`. `get_owned_transaction()` enforces tenant isolation:

- `CUSTOMER` — only transactions where `customer_id` matches their own
- `PROVIDER` — only transactions where their `provider_id` appears in
  `TransactionParticipant`
- `ADMIN` — everything

A transaction that exists but belongs to someone else returns **404**, not
403, so transaction ids cannot be enumerated by probing status codes.

`servicemesh/api/simulation.py::SimulationController` deserves a specific
mention: it detects whether providers run in-process or as separate services
(`PROVIDER_TRANSPORT`) and routes admin failure-injection requests
accordingly — either mutating the local `FailureSimulator` object directly, or
calling the organization's own `/_sim` HTTP API. An earlier version always
took the in-process path, so failure injection silently did nothing when
providers ran as separate processes. It now reports which `channel` it used,
and raises loudly if the organization is unreachable, rather than succeeding
at nothing.

---

## 11. Why not Temporal (yet)

Evaluated and deliberately deferred. Temporal solves durable workflow
execution well, but it would own the workflow while ServiceMesh needs to own
the *domain model* — the transaction, its policy decisions, compatibility
verdicts and recovery rationale all need to be queryable from ServiceMesh's
own relational schema, because that schema is the artefact the research
evaluation measures. Running both would create two sources of truth about "is
this transaction actually here."

`orchestration/workflow.py::Workflow.drive()` is written to the same contract
a Temporal activity needs: it is idempotent to call, always computes the next
action from persisted state, and can be invoked from anywhere — a fresh
request, a cron-style resume job, an operator clicking "resume" days later.
That is the clean integration boundary this document promised in the README:
adopting Temporal later means wrapping `drive()` calls as activities, not
rewriting the domain layer.

---

## 12. Known architectural limitations

- **Sync DB inside an async event loop.** Provider I/O is `async`;
  SQLAlchemy sessions are synchronous. FastAPI runs sync dependencies in a
  threadpool, so this is adequate at the scale the demo targets, but a
  blocking DB call inside `Workflow.drive()`'s async steps does block that
  worker's event loop iteration. Not a problem for correctness; would need
  attention (async SQLAlchemy or explicit `run_in_executor`) before high
  concurrency.
- **Single-writer orchestration.** Nothing prevents two callers from calling
  `drive()` on the same transaction concurrently; there is no row-level lock.
  In practice the API only drives a transaction synchronously within one
  request, and `resume_transaction` is operator-initiated, so this has not
  caused an observed issue, but it is not defended against.
- **No migrations.** Alembic is declared as a dependency but no migration
  scripts exist; the schema is created via `Base.metadata.create_all()`. This
  is acceptable for a project at this stage and is called out explicitly
  rather than glossed over.
