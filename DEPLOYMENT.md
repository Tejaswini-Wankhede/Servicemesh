# Deployment

## Docker Compose

1. Copy `.env.example` to `.env`.
2. Set a strong `JWT_SECRET`.
3. Start:

```bash
docker compose up -d --build
```

4. Seed:

```bash
docker compose exec core python -m scripts.seed --reset
```

5. Open `http://localhost:5173`.

Core API: `http://localhost:8000`.

## Optional events

```bash
docker compose --profile events up -d --build
```

This starts Redpanda and its console. Set `EVENT_BACKEND=kafka` if external event publication is desired.

## Production evolution

Use managed PostgreSQL, a proper identity provider, TLS, secret manager, centralized telemetry and a durable workflow runtime such as Temporal when operational scale warrants it. The domain state should remain owned by ServiceMesh.

The current Compose file deliberately does not start unused Temporal, Neo4j, Keycloak, Prometheus or Grafana containers.
