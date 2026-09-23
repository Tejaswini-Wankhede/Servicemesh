# Local Setup

## Requirements

- Python 3.11+
- Node.js 20+
- npm
- PostgreSQL is optional for local development because SQLite is the default.

## Backend

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# Linux/macOS
source .venv/bin/activate
pip install -e ".[dev,ml]"
copy .env.example .env   # Windows
# cp .env.example .env  # Linux/macOS
python -m scripts.seed --reset
uvicorn servicemesh.api.main:app --reload --port 8000
```

## Frontend

```bash
cd frontend
npm ci
npm run dev
```

Open `http://localhost:5173`.

Set `VITE_API_BASE_URL=http://localhost:8000` when Vite needs an explicit API origin.

## Demo

1. Log in as the customer.
2. Create a battery issue for `ORD-100001` / `SN-AX14-0001`.
3. The backend performs purchase, OEM, warranty, coverage, provider, compatibility, supplier reservation and service booking.
4. The transaction pauses at `WAITING` for real service-centre input.
5. Log in as the service-centre account.
6. Accept, diagnose, start repair and complete repair.
7. The backend independently verifies completion and closes the transaction.
8. Return to the customer transaction page; the persisted timeline and notifications update.

## Failure demo

Use the Admin → Failure simulation screen to inject provider failures. Then create a transaction and observe persisted failures/recovery in the transaction page.
