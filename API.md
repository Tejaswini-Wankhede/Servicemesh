# API Guide

Base URL: `http://localhost:8000`

## Authentication

`POST /api/v1/auth/login`

Returns a signed JWT. Send it as `Authorization: Bearer <token>`.

## Customer

- `GET /api/v1/transactions`
- `POST /api/v1/transactions`
- `POST /api/v1/transactions/natural-language`
- `GET /api/v1/transactions/{id}`
- `GET /api/v1/transactions/{id}/status`
- `GET /api/v1/transactions/{id}/history`
- `GET /api/v1/transactions/{id}/notifications`
- `POST /api/v1/transactions/{id}/resume`
- `POST /api/v1/transactions/{id}/cancel`

## Provider portals

- `GET /api/v1/providers/me`
- `GET /api/v1/providers/me/jobs`
- `POST /api/v1/providers/me/transactions/{id}/service-decision?action=ACCEPT|REJECT`
- `POST /api/v1/providers/me/transactions/{id}/repair`
- `POST /api/v1/providers/me/transactions/{id}/supplier-dispatch`

## Admin

- `GET /api/v1/admin/metrics`
- `GET /api/v1/providers`
- `GET /api/v1/admin/customers`
- `GET /api/v1/admin/recovery-matrix`
- `GET /api/v1/admin/policies`
- `GET /api/v1/admin/simulation`
- `POST /api/v1/admin/simulation`
- `POST /api/v1/admin/simulation/reset`

Swagger/OpenAPI remains available at `/docs` for developer inspection.
