# Security

- JWT bearer authentication with issuer and expiry validation.
- PBKDF2-SHA256 password hashing with per-password random salt.
- Backend RBAC; frontend route hiding is not the security boundary.
- Customer transaction isolation returns 404 for another customer's transaction.
- Provider access is scoped to the provider ID in the authenticated user record.
- Provider-side API credentials are environment variables.
- `.env` is ignored; `.env.example` contains no secrets.
- Mutating organization calls use idempotency keys.
- Audit events and domain events are persisted.
- Input schemas constrain transaction and portal payloads.
- CORS is explicitly configured for local UI origins.

The included demo credentials are synthetic and must not be used in production.
Production deployment still requires secret rotation, TLS, external identity management, rate limiting and hardened infrastructure.
