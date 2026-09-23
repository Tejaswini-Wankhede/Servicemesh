# 20-Day Execution Plan

This plan is presented in the dependency order actually followed while
building this project, with each day's planned scope marked against what was
genuinely completed, partially completed, or not reached. It is not a
retroactively rewritten plan describing execution as if it were pre-planned
perfectly — where the actual build order diverged from a clean 20-day
sequence (because real bugs were found and fixed out of planned order), that
is noted rather than hidden.

| Day | Planned scope | Status |
|---|---|---|
| 1 | Repository skeleton, `pyproject.toml`, core config/db/enums | **Done** |
| 2 | ORM models (19 tables), verified schema creation | **Done** |
| 3 | Five simulated organizations: models + FastAPI apps, distinct API shapes | **Done** |
| 4 | Failure simulation framework (`providers/common/sim.py`), verified per-service | **Done** |
| 5 | Synthetic dataset generator + seeder across all six databases | **Done**; two real bugs found and fixed (FK ordering, contract-number collision) |
| 6 | Password hashing / JWT primitives | **Done**; bcrypt/passlib incompatibility found and fixed (switched to pbkdf2_sha256) |
| 7 | Transport layer + adapter base contract | **Done**; `success` vs business-answer distinction established here |
| 8 | Five concrete provider adapters, verified against live services including simulated failures | **Done** |
| 9 | Compatibility, policy and selection engines, verified against seeded data | **Done** |
| 10 | Event bus (transactional outbox) + idempotency key derivation | **Done** |
| 11 | State machine (`state`/`resume_state` separation) | **Done** |
| 12 | Recovery engine (R1–R8 matrix), verified against 12 failure contexts including "no permanent failure is ever retried" | **Done** |
| 13 | Orchestrator + workflow steps, first end-to-end happy-path transaction | **Done** |
| 14 | Full test suite for the 10+ demonstration scenarios | **Done**; three real orchestration bugs found (infinite retry loop from read-only operations, misattributed failures during provider fallback, permanent exclusion of transiently-failed providers on resume) |
| 15 | REST API: auth, RBAC, transaction lifecycle, provider portal, admin dashboard | **Done** |
| 16 | GenAI natural-language intake (rule-based + optional LLM cross-check) | **Done** |
| 17 | Docker: five Dockerfiles, compose stack, `.env.example` | **Written but not booted** — no Docker daemon available in the build environment (see `docs/deployment.md`) |
| 17b | (unplanned, added after finding the Docker gap) `http` transport verified with real detached processes over real sockets | **Done** — closed the largest verification gap found mid-project |
| 18 | React frontend: customer/provider/admin portals, Transaction Detail page | **Done**; verified against the live API (all consumed endpoints checked), not verified in an actual browser |
| 19 | ML pipeline (dataset, train, predict) + research evaluation harness (baseline, experiment runner, plots) | **Done**; one real methodology bug found and fixed (the first baseline was a strawman — it required an idempotency key the sequential baseline should not have been sending correctly) |
| 20 | Documentation: README, architecture, API, database, deployment, research, novelty, patent, business, this plan | **Done**, this document included |

## What this plan explicitly does not claim

- It does not claim every day was executed as a clean, sequential 20-day
  calendar — the actual work happened across several extended sessions, with
  the ordering above reflecting *dependency order*, which is what the
  original brief asked for ("first make the core backend and database
  functional, then simulated organizations, then Service Transactions...").
- It does not claim zero rework. Several items above list a bug found during
  a later day's testing that required returning to an earlier component
  (e.g. the idempotency fingerprinting bug found while testing recovery
  scenarios required a fix in `orchestration/idempotency.py`, which is
  conceptually a Day 10 file, discovered during Day 14 testing). This is
  reported because it happened, not smoothed over.
- Day 17 (Docker) is explicitly marked as not fully completed — the compose
  stack was written and is believed correct by inspection, but was never
  booted, because no Docker daemon was available in the environment this
  project was built in. This is the single largest gap between "written" and
  "verified" in the entire project and is called out here, in
  `docs/deployment.md`, and in the README rather than only in one place.

## Features deliberately left unfinished, and why (not silently dropped)

- **Alembic migrations**: declared as a dependency, no migration scripts
  written. Schema managed via `create_all` + `--reset`. Acceptable at this
  project's current stage; would block a production deployment with real
  data and should be the first Version 1 task (see `docs/business.md`).
- **`provider_metric_snapshots` rollup job**: the table exists, no scheduled
  job populates it. The ML training pipeline currently trains on generated
  synthetic data rather than this table's (currently empty) real operational
  history.
- **OpenTelemetry distributed tracing**: correlation ids propagate end-to-end
  and are logged everywhere (see `docs/architecture.md` §10), which delivers
  the practical "follow one transaction across every organization's logs"
  requirement, but no OpenTelemetry spans/exporters are wired up. Structured
  logging with correlation ids was judged sufficient for this project's
  demonstration needs; full distributed tracing is listed as Version 2 scope.
- **Prometheus / Grafana**: not added. The admin dashboard computes metrics
  live from the database on each request (`GET /api/v1/admin/metrics`), which
  is adequate at this scale; a real deployment at higher transaction volume
  would benefit from time-series metrics infrastructure, which was
  deliberately not added here to avoid infrastructure for appearance.
- **Kubernetes / Terraform**: not added, per the brief's own instruction not
  to add infrastructure without a demonstrated need this project's scale
  actually has.
