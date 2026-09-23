# API Documentation

This document is grounded in the routes actually registered on the running
FastAPI application (`servicemesh.api.main:app`), extracted via
`app.openapi()` at documentation time — not written from memory. The live,
interactive version is always available at `GET /docs` (Swagger UI) or
`GET /openapi.json` (raw spec) on a running instance.

Base URL (local): `http://localhost:8000`

---

## Authentication

All endpoints except `/health`, `/`, and `POST /api/v1/auth/login` require a
bearer token:

```
Authorization: Bearer <token>
```

### `POST /api/v1/auth/login`

```json
// request
{"email": "aarti@example.com", "password": "customer123"}

// 200 response
{
  "access_token": "eyJ...",
  "token_type": "bearer",
  "expires_in_minutes": 240,
  "role": "CUSTOMER",
  "user_id": "...",
  "full_name": "Aarti Deshpande",
  "customer_id": "...",
  "provider_id": null,
  "provider_code": null
}
```

Wrong password and unknown email return the **identical** `401` response —
`{"detail": {"error": "invalid_credentials", "message": "incorrect email or password"}}`
— so the endpoint cannot be used to enumerate accounts
(`test_login_does_not_leak_whether_account_exists`).

### `GET /api/v1/auth/me`

Returns the caller's own user record. Any authenticated role.

---

## Authorization model

Three roles: `CUSTOMER`, `PROVIDER`, `ADMIN`. Enforcement happens **only** on
the backend (`servicemesh/api/deps.py`) — the frontend hiding a button is a UX
courtesy, not a security boundary.

| Rule | Enforced by |
|---|---|
| A customer sees only their own transactions | `get_owned_transaction()` filters by `customer_id` |
| A provider sees only operations of their own organization | filters by `provider_id` via `TransactionParticipant` |
| An admin sees everything | role check short-circuits the filter |
| Accessing another tenant's transaction id | returns **404**, not 403 (id enumeration protection) |
| Role-gated endpoints (`/admin/*`) | `require_admin`, `require_provider_or_admin` dependencies |

---

## Transactions

### `POST /api/v1/transactions`

Create and (unless `defer_start`) immediately drive a Service Transaction.
`CUSTOMER` role required (or `ADMIN` with `?customer_id=`).

```json
{
  "order_ref": "ORD-100001",
  "serial_number": "SN-AX14-0001",
  "issue_type": "BATTERY_FAILURE",
  "issue_description": "Battery swells and shuts down after 20 minutes",
  "urgency": "NORMAL",
  "requested_component_sku": null,
  "auto_repair": false,
  "defer_start": false
}
```

Returns `201` with a full `TransactionDetail` (see below). `auto_repair` is
only honoured for `ADMIN` callers — a customer cannot fake repair completion
at the service centre.

### `POST /api/v1/transactions/natural-language`

```json
{
  "text": "My laptop battery is swelling and shuts down after twenty minutes. Order ORD-100001, serial SN-AX14-0001.",
  "auto_repair": false
}
```

Runs the rule-based/LLM extractor (`servicemesh/genai/extract.py`) first. If
confidence is below threshold and no `serial_number`/`order_ref` override was
supplied, returns **422** with `{"error": "extraction_uncertain", "detail":
{...extraction...}}` rather than guessing. On success, behaves like the
structured endpoint — the extraction only supplies suggested field values; the
deterministic engines still run in full.

### `GET /api/v1/transactions`

List transactions visible to the caller. Query params: `state`, `limit`
(default 50, max 200), `offset`.

### `GET /api/v1/transactions/{id}`

Full `TransactionDetail`:

```
id, reference, correlation_id, state, resume_state, progress_percent,
customer_id, customer_name, order_ref, serial_number, issue_type, urgency,
outcome, outcome_reason, sla_due_at, sla_breached,
requires_manual_intervention, retry_total, created_at, updated_at, closed_at,
issue_description, raw_request_text, nlp_extraction,
purchase_evidence, product_evidence, warranty_evidence, coverage_evidence,
model_code, required_component_type, selected_component_sku,
service_provider_code, supplier_code, part_reservation_ref,
service_booking_ref, excluded_provider_codes, excluded_component_skus,
timeline[], state_history[], operations[], participants[],
failures[], recovery_actions[], decisions[], events[]
```

Every array is backed by a real query over persisted rows — see
`servicemesh/api/serializers.py::to_detail()`. The `timeline` array in
particular is derived, not stored: each of the 14 happy-path states is marked
`COMPLETED` / `CURRENT` / `FAILED` / `PENDING` / `SKIPPED` based on
`TransactionStateHistory` and `FailureRecord` rows for that specific
transaction (`build_timeline()`).

### `GET /api/v1/transactions/{id}/status`

Lightweight polling endpoint: state, resume_state, progress, outcome, SLA
flag, `updated_at`. Used by the frontend's detail-page auto-refresh instead of
re-fetching the full detail payload every 4 seconds.

### `GET /api/v1/transactions/{id}/history`

The same sub-arrays as the detail payload (`timeline`, `state_history`,
`operations`, `failures`, `recovery_actions`, `decisions`, `events`) without
the top-level transaction fields — convenient for an audit export.

### `POST /api/v1/transactions/{id}/resume`

Operator- or customer-initiated resume of a transaction sitting in
`ESCALATED`, `WAITING`, `RETRYING` or `TIMEOUT`. Returns `409` if the
transaction is already terminal. Internally calls
`orchestration/service.py::resume_transaction()`, which:

1. clears provider exclusions caused by **transient** faults only (permanent
   exclusions, e.g. an unauthorized OEM partner, are kept — see
   `_clear_transient_exclusions()`),
2. transitions back to `resume_state`,
3. drives the workflow forward.

### `POST /api/v1/transactions/{id}/cancel`

Cancels and compensates: releases any part reservation, cancels any service
booking, transitions to `CANCELLED`. Returns `409` if already terminal.

### `GET /api/v1/transactions/{id}/customers/me`

Returns the `Customer` record for a transaction the caller can already see —
a convenience for the frontend rather than a distinct authorization surface.

---

## Provider portal

### `GET /api/v1/providers/me`

The caller's own organization, with observed reliability stats
(`success_rate`, `avg_latency_ms`, `available_capacity` computed live from
`total_operations` / `successful_operations` / `capacity_used`).

### `GET /api/v1/providers/me/jobs`

Operations belonging to the caller's organization **only** — filtered by
`user.provider_id` taken from the token, never from a request parameter. This
is the endpoint `test_provider_sees_only_its_own_organizations_operations`
checks directly: a service centre's job list contains no `RESERVE_PART`
operations, a supplier's contains no `BOOK_SERVICE` operations.

### `POST /api/v1/providers/me/transactions/{id}/repair`

```json
{"status": "REPAIRING", "notes": "diagnosis confirms battery fault"}
```

`status` is one of `CHECKED_IN | DIAGNOSING | AWAITING_PART | REPAIRING |
COMPLETED`. Only the service centre actually assigned to the transaction may
call this (`403` otherwise); `409` if there is no booking yet. On
`COMPLETED`, the workflow advances through `REPAIR_COMPLETED ->
SERVICE_VERIFIED -> CLOSED` automatically — completion is *independently
re-verified* by reading the service centre's own booking record back
(`CONFIRM_COMPLETION`), not merely assumed because ServiceMesh sent the
update.

### `GET /api/v1/providers`

`ADMIN` only. All registered providers with reliability stats. Optional
`?kind=` filter.

---

## Admin / operations

### `GET /api/v1/admin/metrics`

Every field is computed from a live query — none is a stored or cached
figure:

```
total_transactions, by_state{}, completed, failed, rejected, escalated,
in_progress, recovering, sla_breaches, manual_interventions, total_retries,
total_failures, total_recovery_actions, recovery_success_rate,
duplicate_operations_prevented, avg_resolution_seconds,
avg_operations_per_transaction, completion_rate, generated_at
```

`duplicate_operations_prevented` counts `ProviderOperation` rows where
`attempt_count > 1`, an idempotency key was present, and the final status is
`SUCCEEDED` — i.e. operations that *would* have double-executed at the
organization without the idempotency layer.

### `GET /api/v1/admin/customers`

All customers (for the admin "create on behalf of" flow).

### `GET /api/v1/admin/recovery-matrix`

Returns the failure/recovery matrix as implemented, read live from
`engines/recovery.py::describe_matrix()` — documentation generated from code,
not maintained by hand.

### `GET /api/v1/admin/policies`

Returns the registered policy rule sets and their rule names, read live from
`PolicyEngine().describe()`.

### `GET /api/v1/admin/simulation`

Current failure-simulation state of every organization, plus which `channel`
(`inproc` or `http`) is being used to control them and the list of available
modes.

### `POST /api/v1/admin/simulation`

```json
{"service": "parts_supplier", "mode": "TEMPORARILY_UNAVAILABLE",
 "operation": "inventory.check", "count": 2, "latency_seconds": 0}
```

Routed through `servicemesh/api/simulation.py`. Over HTTP this calls the
organization's own `/_sim/mode` API and raises `502` if the organization is
unreachable, rather than silently succeeding at nothing.

### `POST /api/v1/admin/simulation/reset`

Resets one (`?service=`) or all organizations to `NORMAL`.

---

## Error format

Every error response is `{"detail": {"error": "<code>", "message": "<human text>", ...}}`
via FastAPI's `HTTPException(detail={...})` convention, with one exception:
unhandled server errors return `{"error": "internal_error", "message": "...",
"correlation_id": "..."}` directly (see `unhandled_exception_handler` in
`api/main.py`), so an operator can grep logs by the same correlation id shown
to the caller.

Standard status codes used:

| Code | Meaning here |
|---|---|
| 400 | malformed simulation mode, missing customer profile |
| 401 | missing/invalid/expired token |
| 403 | authenticated but wrong role, or wrong provider organization |
| 404 | resource does not exist **or** caller is not entitled to see it |
| 409 | conflicting state transition (already terminal, no booking yet) |
| 422 | request body failed schema validation, or NL extraction was uncertain |
| 502 | a provider organization was unreachable during simulation control or a repair update |

---

## Correlation ids and observability

Every response carries `X-Correlation-Id` and `X-Response-Time-Ms` headers
(`api/main.py::correlation_middleware`). The correlation id is generated per
HTTP request unless the caller supplies one, and every `ServiceTransaction`
carries its own persistent `correlation_id` that is forwarded to every
provider adapter call as `X-Correlation-Id` — so one transaction's activity
can be grepped across ServiceMesh's logs and every organization's logs using
the same value.

---

## Regenerating this reference

The canonical source of truth is always the live spec:

```bash
curl -s http://localhost:8000/openapi.json | python -m json.tool > openapi.json
```

or interactively at `/docs`. This document is a curated companion, not a
replacement — every route listed above was cross-checked against the
generated spec at the time of writing, and the response field lists were
taken directly from `servicemesh/api/schemas.py`.
