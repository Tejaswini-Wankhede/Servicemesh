# Deployment Documentation

## Verification status — read this first

| Path | Status |
|---|---|
| Local, `PROVIDER_TRANSPORT=inproc` | **Verified.** 54 automated tests pass against this configuration. |
| Local, `PROVIDER_TRANSPORT=http`, five separate uvicorn processes + core as a sixth | **Verified by manual execution.** Five organizations started as real processes on real sockets (ports 8101–8105), core API started as a seventh process on port 8000, full transaction closed end-to-end, RBAC enforced (401/403 confirmed), failure injection confirmed to genuinely cross process boundaries (two real HTTP 503s, two retries, success on the third attempt). |
| Docker Compose | **Not verified.** No Docker daemon was available in the environment this was built in. The compose file has been validated for YAML correctness and the service dependency graph is checked by hand, but the stack has never actually been booted. Treat every claim below about Docker as "should work, unconfirmed" rather than "confirmed." |
| PostgreSQL | **Not verified.** Every run in this project, including the "verified" ones above, used SQLite. The code path for PostgreSQL (`psycopg2`, `DATABASE_URL=postgresql+psycopg2://...`) is standard SQLAlchemy and the schema has no SQLite-specific constructs other than the `PRAGMA foreign_keys` connection hook (which only fires for SQLite URLs — see `core/db.py::_set_sqlite_pragmas`), but it has not been run against a real Postgres instance. |

If you deploy this for a demonstration, **run the local `http` multi-process
setup described below first** — it is the closest verified approximation to
Docker, and if something is going to break in the containerized version, this
is the fastest way to find it before committing to a container build.

---

## Option A — single process (fastest, verified)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,ml]"
cp .env.example .env          # PROVIDER_TRANSPORT=inproc is the default
python -m scripts.seed --reset
uvicorn servicemesh.api.main:app --reload
```

The five organizations run inside the same process as the API, using
`httpx.ASGITransport` rather than real sockets. This is not a mock — full
FastAPI routing, validation, auth and each organization's own SQLite database
are exercised — but it does not exercise the network, DNS, or
container-boundary failure modes that a real deployment has.

## Option B — separate processes over real HTTP (verified)

This is what actually got exercised end-to-end this session, and is the
closest thing to a Docker deployment that has been proven to work.

```bash
export PROVIDER_DATA_DIR=./data-http
mkdir -p $PROVIDER_DATA_DIR

uvicorn providers.marketplace.app:app    --port 8101 &
uvicorn providers.manufacturer.app:app   --port 8102 &
uvicorn providers.warranty.app:app       --port 8103 &
uvicorn providers.service_centre.app:app --port 8104 &
uvicorn providers.parts_supplier.app:app --port 8105 &

# give them a few seconds, then confirm:
for p in 8101 8102 8103 8104 8105; do curl -s localhost:$p/health; echo; done

export PROVIDER_TRANSPORT=http
export DATABASE_URL="sqlite+pysqlite:///$PROVIDER_DATA_DIR/core.db"
export MARKETPLACE_URL=http://127.0.0.1:8101
export MANUFACTURER_URL=http://127.0.0.1:8102
export WARRANTY_URL=http://127.0.0.1:8103
export SERVICE_CENTRE_URL=http://127.0.0.1:8104
export PARTS_SUPPLIER_URL=http://127.0.0.1:8105
export JWT_SECRET=$(python3 -c "import secrets;print(secrets.token_urlsafe(48))")

python -m scripts.seed --reset
uvicorn servicemesh.api.main:app --port 8000
```

Confirm the whole chain: `curl localhost:8000/health` should report
`"provider_transport": "http"`, and creating a transaction via the API should
result in `participants` from all five organizations.

## Option C — Docker Compose (untested, documented as designed)

```bash
cp .env.example .env
# Edit .env: set JWT_SECRET to a real random value. Compose will refuse to
# start core without it (see docker-compose.yml: JWT_SECRET is `:?required`).

docker compose up -d --build
docker compose exec core python -m scripts.seed --reset
```

What the compose file does, by design:

- One `postgres:16-alpine` container. `deploy/init-databases.sql` runs on
  first boot and creates five additional databases
  (`org_marketplace`, `org_manufacturer`, `org_warranty`,
  `org_service_centre`, `org_parts_supplier`) alongside the default
  `servicemesh` database — six databases, one Postgres instance, which is a
  deployment convenience, **not** a retreat from the "each organization owns
  its own database" principle: each organization still connects to its own
  distinct database name and cannot query another's tables.
- Five provider containers built from the single `deploy/Dockerfile.provider`
  image, parameterised by `PROVIDER_MODULE` and `PROVIDER_PORT` build args.
  One Dockerfile, five images — this only avoids five nearly-identical files;
  each container still runs one organization with its own environment
  variables and its own database connection string.
- The `core` container waits on all five provider containers plus Postgres
  (`depends_on: condition: service_healthy` / `service_started`) before
  starting, and receives `PROVIDER_TRANSPORT=http` with the in-cluster DNS
  names (`http://marketplace:8101`, etc.) as its provider URLs.
- The `frontend` container is a two-stage build: Node builds the Vite bundle,
  then an `nginx:1.27-alpine` serves the static output with a rewrite rule
  (`deploy/nginx.conf`) so client-side routing works on refresh.
- `redpanda` and `redpanda-console` are behind the `events` Compose **profile**
  and are not started by `docker compose up` alone — matching
  `EVENT_BACKEND=memory` being the functional default described in the README.

**What to check if this doesn't come up cleanly**, since it hasn't been run:

1. `docker compose logs core` — most likely failure point is a provider not
   yet healthy when core starts its first request; the `depends_on` health
   checks should prevent this, but if they don't, add a startup retry loop
   around the first seed call.
2. Confirm the five provider healthchecks actually pass
   (`docker compose ps` should show `healthy`, not just `running`) — each
   provider's healthcheck curls its own `/health`.
3. `docker compose exec core python -m scripts.seed --reset` must succeed
   before any transaction can be created — the compose file does not run this
   automatically on first boot, matching the "explicit seed step" pattern used
   in every other run mode in this project.

---

## Environment variables

Full list in `.env.example`. The ones that differ meaningfully between local
and Docker:

| Variable | Local default | Docker value |
|---|---|---|
| `DATABASE_URL` | `sqlite+pysqlite:///./data/servicemesh.db` | `postgresql+psycopg2://...@postgres:5432/servicemesh` |
| `PROVIDER_TRANSPORT` | `inproc` | `http` |
| `MARKETPLACE_URL` etc. | `http://marketplace:8101` (unused in inproc mode) | `http://marketplace:8101` (real, resolved via Docker's internal DNS) |
| `JWT_SECRET` | insecure dev default | **required**, compose fails fast without it |

No secrets are baked into any image. `.env` is `.gitignore`d;
`.dockerignore` excludes it from the build context as well.

---

## Rebuilding after a code change

```bash
docker compose up -d --build          # rebuilds changed images only
docker compose restart core           # if only servicemesh/ changed
```

The provider images and the core image are built from the same source tree
but different Dockerfiles, so a change to `providers/parts_supplier/app.py`
requires rebuilding the `parts-supplier` service specifically (or just
`--build` everything, which is simpler and correctly picks up the change via
layer caching).

---

## Cloud readiness

Not deployed to a cloud environment. What would need to change:

- **Secrets**: `JWT_SECRET` and the Postgres credentials should move to the
  target platform's secret manager rather than `.env` file injection.
- **Database**: the compose Postgres container would be replaced by a managed
  instance (RDS, Cloud SQL, etc.); `DATABASE_URL` is the only thing that needs
  to change, since the application code has no Postgres-specific assumptions
  beyond standard SQLAlchemy.
- **Provider services**: each is stateless except for its own database
  connection, so they are straightforward to run as independent services
  (ECS tasks, Cloud Run services, or Kubernetes deployments) behind whatever
  internal service discovery the platform provides — `PROVIDER_TRANSPORT=http`
  plus the five `*_URL` variables are the only integration surface.
- **Core API**: same story — stateless aside from its database connection,
  horizontally scalable in principle, with the caveat noted in
  `docs/architecture.md` §12 about no row-level locking on concurrent
  `drive()` calls against the same transaction. A single active writer per
  transaction (true today, since only one API request or one operator resume
  drives a transaction at a time) is currently what keeps this safe; it would
  need explicit locking before running multiple API replicas that might both
  pick up work on the same transaction.
- **TLS**: not configured anywhere in this repository; would sit at a load
  balancer or ingress layer in front of the nginx/uvicorn processes.

Kubernetes and Terraform were deliberately not added — see the README's
"deliberately deferred" section for the reasoning that applies equally here:
neither would have been implemented properly in the time available, and the
brief explicitly asks that infrastructure not be added for appearance.
