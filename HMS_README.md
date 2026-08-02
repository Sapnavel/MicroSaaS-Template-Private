# Hospital Management & Appointment Booking System

Multi-tenant, real-time HMS. Start with `docs/ARCHITECTURE.md` for the full design
rationale — this file is just "how do I run it."

## What's implemented vs. scaffolded

**Implemented (production-shaped):**
- Multi-tenant schema for all 10 modules (`database/schema.sql`)
- RBAC/ABAC auth foundation, JWT, PHI field-level encryption, immutable audit log
- **Atomic tri-resource booking engine** — `backend/app/services/scheduling_engine.py`
- **Emergency preemption algorithm** — `backend/app/services/emergency_engine.py`
- Live queue WebSocket channel — `backend/app/websocket/queue_board.py`
- Full docker-compose infra (Postgres, Redis, RabbitMQ, backend, frontend)

**Scaffolded (schema + router stub with TODOs, not yet implemented):**
Auth endpoints, Patient Master Index/dedup, Consultation/Prescription engine,
Lab, Pharmacy, Wards/OT, Billing/Insurance, Notifications, Dashboard.
Each stub router (`backend/app/routers/*.py`) has a docstring describing exactly
what to build and which schema tables back it. Build order recommendation is in
`docs/ARCHITECTURE.md` section 9.

## Run locally

```bash
cp .env.example .env
# generate a PHI encryption key and put it in .env as PHI_ENCRYPTION_KEY:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

docker-compose up -d
curl http://localhost:8000/health
```

Frontend: http://localhost:3000
API docs: http://localhost:8000/docs

## Run without Docker

```bash
# Postgres, Redis, RabbitMQ must be running locally first.

cd backend
pip install -r requirements.txt
psql "$DATABASE_URL" -f ../database/schema.sql   # initial schema (authoritative)
uvicorn app.main:app --reload

cd ../frontend
npm install
npm run dev
```

Note on migrations: `database/schema.sql` is the authoritative initial schema
(it includes `EXCLUDE USING gist` constraints and the `btree_gist` extension
that Alembic's autogenerate does not reliably capture). Apply it directly for
a fresh database; use `alembic revision --autogenerate` for changes *after*
that baseline, and hand-check any new exclusion constraints it should add.

## Try the booking engine

```bash
# 1. Book a normal appointment (doctor + room, no equipment)
curl -X POST http://localhost:8000/api/v1/appointments \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"patient_id":"...","doctor_id":"...","room_id":"...","start_time":"2026-08-01T10:00:00Z","duration_minutes":15}'

# 2. Book an emergency (Triage 1/2) — preempts if nothing is free
curl -X POST http://localhost:8000/api/v1/emergency/book \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"patient_id":"...","specialty_id":1,"triage_level":1}'
```

## Validation

```bash
ruff check backend/ && pytest backend/tests
cd frontend && npm run lint && npm run type-check
docker-compose config
```

(No `backend/tests/` yet — add them alongside each module as it's implemented,
per `skills/TESTING.md`.)
