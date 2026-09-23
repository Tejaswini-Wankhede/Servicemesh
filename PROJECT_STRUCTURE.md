# Project Structure

```text
servicemesh/
├── servicemesh/              # core domain, API, adapters, engines, workflow
│   ├── api/
│   ├── adapters/
│   ├── core/
│   ├── engines/
│   ├── events/
│   ├── genai/
│   ├── ml/
│   └── orchestration/
├── providers/                # independently simulated organizations
│   ├── marketplace/
│   ├── manufacturer/
│   ├── warranty/
│   ├── service_centre/
│   └── parts_supplier/
├── frontend/                 # React/Vite product UI
├── scripts/                  # seed and dataset generation
├── tests/                    # API, scenario and ML tests
├── experiments/              # baseline vs ServiceMesh experiment harness
├── artifacts/                # model/report artifacts, no secrets
├── deploy/                   # Dockerfiles and database bootstrap
├── migrations/               # database migration SQL
├── docs/                     # supporting design/research documents
├── ARCHITECTURE.md
├── API.md
├── SETUP.md
├── TESTING.md
├── SECURITY.md
└── DEPLOYMENT.md
```
