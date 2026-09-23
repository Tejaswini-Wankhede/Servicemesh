# Testing

## Automated suites

- `tests/test_api.py`: authentication, RBAC, API payloads, customer isolation, provider portals, simulation and new portal actions.
- `tests/test_scenarios.py`: real adapter/provider scenarios including warranty rejection, provider fallback, compatibility fallback, supplier failure, idempotency and recovery.
- `tests/test_ml.py`: synthetic dataset determinism and ML selection assistance.

## Commands

```bash
python -m pytest -q tests/test_api.py
python -m pytest -q tests/test_scenarios.py
python -m pytest -q tests/test_ml.py
```

## Verified in this delivery

- API suite: 29 passed.
- Scenario suite: 18 passed.
- ML suite: 9 passed.

These were executed against the repository after modification. Docker was not executable in the delivery environment because the Docker CLI/daemon was unavailable. The frontend npm registry was also unavailable, so a clean Vite build could not be honestly reported as tested.

## Manual acceptance checklist

- customer login
- customer transaction creation
- purchase/product/warranty/coverage evidence
- provider selection
- service-centre accept/reject
- technician/repair updates
- supplier reservation and dispatch
- customer notifications
- admin transaction inspection
- supplier timeout/fallback
- duplicate reservation protection
- timeout-after-commit reconciliation
