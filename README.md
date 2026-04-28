# Playto Payout Engine

A minimal payout engine for Playto Pay — handling merchant balances, payout requests, background processing, and the concurrency/idempotency guarantees that real payment systems require.

## Architecture

```
┌─────────────┐    POST /api/v1/payouts/    ┌──────────────────┐
│  React UI   │ ─────────────────────────▶  │  Django + DRF    │
│  (Vite)     │ ◀─────────────────────────  │  (API Server)    │
└─────────────┘                             └────────┬─────────┘
                                                     │ enqueue
                                                     ▼
                                            ┌──────────────────┐
                                            │  Celery Worker   │
                                            │  (process_payout)│
                                            └────────┬─────────┘
                                                     │ reads/writes
                                                     ▼
                                    ┌──────────────────────────────┐
                                    │         PostgreSQL            │
                                    │  merchants | ledger_entries   │
                                    │  payouts   | idempotency_keys │
                                    └──────────────────────────────┘
                                                     ▲
                                            ┌────────┘
                                            │ schedules
                                   ┌────────┴─────────┐
                                   │  Celery Beat      │
                                   │  (retry stuck     │
                                   │   payouts / 30s)  │
                                   └──────────────────┘
```

## Quickstart (Docker — recommended)

```bash
git clone <your-repo-url>
cd playto-payout

docker-compose up --build
```

- **Frontend:** http://localhost:3000
- **API:** http://localhost:8000/api/v1/
- **Admin:** http://localhost:8000/admin/

The backend container automatically runs migrations and seeds test data on startup.

---

## Manual Setup (without Docker)

### Prerequisites
- Python 3.12+
- PostgreSQL 14+
- Redis 7+
- Node.js 20+

### Backend

```bash
cd backend

# Create virtualenv
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment (or set env vars directly)
export DB_NAME=playto_db
export DB_USER=postgres
export DB_PASSWORD=postgres
export DB_HOST=localhost
export DB_PORT=5432
export REDIS_URL=redis://localhost:6379/0

# Create database
createdb playto_db

# Run migrations
python manage.py migrate

# Seed test merchants
python manage.py seed_data

# Start API server
python manage.py runserver
```

### Celery Workers (in separate terminals)

```bash
# Worker — processes payouts
celery -A playto worker --loglevel=info

# Beat — retries stuck payouts every 30s
celery -A playto beat --loglevel=info
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Visit http://localhost:3000

---

## API Reference

### List merchants
```
GET /api/v1/merchants/
```

### Merchant detail + balance
```
GET /api/v1/merchants/{id}/
```
Returns `available_paise`, `held_paise`, `total_credits_paise`, bank accounts.

### Ledger entries
```
GET /api/v1/merchants/{id}/ledger/
```

### Merchant payouts
```
GET /api/v1/merchants/{id}/payouts/
```

### Create payout ⚠️ Critical path
```
POST /api/v1/payouts/
Headers:
  Content-Type: application/json
  Idempotency-Key: <uuid>     ← required

Body:
{
  "merchant_id": "uuid",
  "amount_paise": 10000,      ← ₹100 in paise (integer, never float)
  "bank_account_id": "uuid"
}

Responses:
  201 — payout created
  400 — missing/invalid fields
  404 — merchant or bank account not found
  409 — concurrent conflict (retry)
  422 — insufficient balance
```

### Payout status
```
GET /api/v1/payouts/{id}/
```

---

## Running Tests

```bash
cd backend
python manage.py test payout_engine
```

Tests cover:
- **Concurrency:** Two simultaneous 60p payouts against 100p balance — exactly one succeeds
- **Idempotency:** Same key returns same response, no duplicate payout created
- **State machine:** Illegal transitions raise ValueError
- **Ledger invariant:** credits - debits = available balance

---

## Technical Highlights

### Money integrity
- All amounts in paise as `BigIntegerField`. No `FloatField`. No `DecimalField`.
- Balance computed via DB-level `SUM()` aggregation, not Python arithmetic on fetched rows.
- Ledger entries are immutable — never updated or deleted, only appended.

### Concurrency
- `SELECT FOR UPDATE` on ledger entries + pending payouts within `transaction.atomic()`.
- Balance check and payout creation happen atomically under the same lock.
- PostgreSQL enforces the lock — no Python-level threading tricks.

### Idempotency
- `Idempotency-Key` UUID header required on all payout requests.
- Keys stored with full response body — replayed exactly on repeat calls.
- Keys scoped per merchant — same key from different merchants treated independently.
- Keys expire after 24 hours.
- Concurrent duplicate requests handled via `IntegrityError` catch on unique constraint.

### State machine
- `LEGAL_TRANSITIONS` dict defines valid transitions. `ValueError` on violation.
- `failed → completed` is structurally impossible (empty transition list).
- Failed payout refund is atomic with status transition — no partial states.

### Retry logic
- Celery Beat runs every 30 seconds, finds payouts stuck in `processing`.
- Max 3 attempts with exponential backoff. Then → `failed` + funds returned.
- `select_for_update(skip_locked=True)` prevents multiple beat workers from double-processing.

### Simulated bank responses
- 70% success → payout completed, debit ledger entry created
- 20% failure → payout failed, credit (refund) ledger entry created
- 10% hang → payout stays in `processing`, retry_stuck_payouts handles it

---

## Project Structure

```
playto-payout/
├── backend/
│   ├── playto/
│   │   ├── settings.py      # Django config + Celery config
│   │   ├── urls.py          # Root URL routing
│   │   └── celery.py        # Celery app
│   ├── payout_engine/
│   │   ├── models.py        # Merchant, LedgerEntry, Payout, IdempotencyKey
│   │   ├── views.py         # API endpoints + concurrency logic
│   │   ├── tasks.py         # Celery tasks (process_payout, retry_stuck_payouts)
│   │   ├── urls.py          # App URL routing
│   │   ├── tests.py         # Concurrency + idempotency tests
│   │   └── management/
│   │       └── commands/
│   │           └── seed_data.py
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   ├── src/
│   │   ├── App.jsx          # Main React app
│   │   ├── index.css        # Styles
│   │   └── main.jsx         # Entry point
│   ├── package.json
│   └── Dockerfile
├── docker-compose.yml
├── EXPLAINER.md
└── README.md
```
