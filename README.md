# Hospital Management & Appointment Booking System

A multi-tenant, real-time Hospital Management System: patient records,
appointment scheduling with database-enforced double-booking prevention,
emergency triage preemption, a live queue/token board, clinical
consultations with a real drug-safety engine, lab workflow, pharmacy FEFO
inventory, ward/bed/OT management, billing & insurance claims, a
notification hub, and an executive dashboard — all ten modules implemented
end-to-end (schema → service → API → UI), not scaffolded.

See `docs/ARCHITECTURE.md` for the full design rationale. This file is "how
do I run it, and what's actually here."

## Features

- **Patients**: Patient Master Index with deterministic + fuzzy duplicate
  detection and a safe merge workflow; a unified cross-module patient
  timeline (appointments, consultations, labs, prescriptions, admissions,
  invoices, merged and sorted).
- **Appointments**: doctor + room + equipment booking, backed by
  Postgres `EXCLUDE USING gist` constraints (not just app-level checks);
  rescheduling; real available-slot computation from doctor shifts; a
  fairness-ordered waiting list; recurring appointments.
- **Emergency care**: Triage 1/2 cases preempt the lowest-cost booked slot
  when nothing is free, with an async reschedule pipeline for the bumped
  patient.
- **Queue & tokens**: live, WebSocket-pushed queue board with
  emergency/priority queue-jumping and automatic downstream wait-time
  recalculation.
- **Consultations & prescriptions**: a real, DB-backed drug-allergy and
  drug-interaction safety engine (tiered BLOCK / OVERRIDE_REQUIRED / INFO
  findings), not a static UI warning.
- **Lab**: order → collect → process → verify → attach workflow with a real
  state machine and report-ready notifications.
- **Pharmacy**: FEFO (first-expired-first-out) dispensing with atomic stock
  deduction, low-stock and expiring-batch alerts.
- **Wards/beds/OT**: admission/transfer/discharge, OT scheduling, the same
  exclusion-constraint discipline as appointments.
- **Billing & insurance**: itemized invoices, tax/discount handling,
  insurance claim state machine, generated PDF receipts.
- **Notifications**: RabbitMQ-backed hub with delivery-status tracking and
  manual retry.
- **Executive dashboard**: occupancy, wait times, revenue, no-show rate, and
  stock alerts — real SQL aggregates, branch- and date-filterable.
- **Security**: JWT auth with signing-key rotation and Redis-backed
  revocation, server-side RBAC/ABAC on every endpoint, PHI field-level
  encryption, a hash-chained tamper-evident audit log, rate limiting,
  security headers.

See `docs/PRESENTATION.md` for the full problem/solution/module rundown and
`docs/PRESENTATION.md` section 11 / "Known limitations" below for what's
deliberately out of scope.

## Technology stack

| Layer | Choice |
|---|---|
| Backend | FastAPI + Python 3.11+ |
| Database | PostgreSQL 15+ (`btree_gist`, `pg_trgm` extensions) |
| ORM | SQLAlchemy 2.0 |
| Async infra | Redis (distributed locks, rate limiting, token revocation), RabbitMQ (event bus + two worker consumers) |
| Frontend | React + TypeScript + Vite |
| Auth | JWT (access + refresh, rotating signing keys) + bcrypt |
| Containerization | Docker Compose (7 services) |

## Architecture summary

See `docs/ARCHITECTURE.md` for the full document. In short: every
conflict-prone resource (doctor time, room time, equipment time, bed
occupancy, OT room time) is protected by a Postgres `EXCLUDE USING gist`
constraint as the authoritative guard, with a Redis distributed lock as a
fast-fail layer in front of it — verified by an automated test that fires 8
concurrent booking requests at the same slot and asserts exactly one
succeeds. Every protected endpoint is gated server-side by role
(`require_role`) and, where resource-scoped, by branch tenancy
(`authorize()`) — hiding a button in the UI is never the actual security
boundary.

## Prerequisites

- Docker + Docker Compose (recommended path), **or** Python 3.11+, Node 18+,
  and locally-running PostgreSQL 15+, Redis, and RabbitMQ for the
  without-Docker path.

## Installation & running (Docker — recommended)

```bash
cp .env.example .env
# Generate a PHI encryption key and put it in .env as PHI_ENCRYPTION_KEY:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

docker compose up -d --build
curl http://localhost:8000/health
```

- Frontend: http://localhost:3000
- API docs (interactive OpenAPI/Swagger): http://localhost:8000/docs

## Running without Docker

```bash
# Postgres, Redis, RabbitMQ must already be running locally.

cp .env.example backend/.env   # NOT repo-root .env -- Settings reads ".env" relative to
                                # the CWD it's instantiated from, and uvicorn/scripts below
                                # both run with backend/ as CWD. Fill in a real
                                # PHI_ENCRYPTION_KEY (see "Environment variables" above) --
                                # running any script against real patient data with the
                                # class default placeholder key will encrypt it in a way
                                # nothing else can ever decrypt back.
cd backend
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)   # or otherwise load backend/.env into this shell --
                                        # psql below doesn't read it automatically
psql "$DATABASE_URL" -f ../database/schema.sql   # authoritative initial schema
python scripts/seed_demo_data.py                  # optional but recommended, see "Demo accounts" below
uvicorn app.main:app --reload

# in a second terminal
cd frontend
npm install
npm run dev
```

## Environment variables

See `.env.example` for the full list with defaults. The ones you actually
need to set for local dev:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | `postgresql+psycopg2://user:pass@host:5432/db` |
| `REDIS_URL` | `redis://host:6379/0` |
| `RABBITMQ_URL` | `amqp://guest:guest@host:5672/` |
| `PHI_ENCRYPTION_KEY` | Fernet key for PHI field encryption (generate as shown above) |
| `HMAC_KEY` | Separate key for deterministic PHI-matching hashes |
| `JWT_SIGNING_KEYS` | `{"k1": "..."}` keyring JSON |
| `VITE_API_URL` | Frontend's backend base URL (`http://localhost:8000` for local dev) |

## Database setup

`database/schema.sql` is the **authoritative** initial schema — it includes
`EXCLUDE USING gist` constraints and the `btree_gist`/`pg_trgm` extensions
that Alembic's autogenerate does not reliably capture. Apply it directly to
a fresh database. `alembic revision --autogenerate` is for changes *after*
that baseline; hand-check any new exclusion constraint it proposes. See
`docs/ER_DIAGRAM.md` for the full schema diagram and relationship notes.

## Migration and seed commands

```bash
# Reference data (drugs + drug interactions -- always safe to re-run)
psql "$DATABASE_URL" -f database/seed_clinical_reference_data.sql

# Demo accounts + a demo branch/doctor/room (idempotent, see below)
docker compose exec backend python scripts/seed_demo_data.py
```

## Test commands

```bash
# Backend (642 tests, run against a real Postgres/Redis/RabbitMQ -- uses its
# own hms_test database/Redis index, safe to run alongside a local Docker
# dev stack, see "Known limitations" below)
cd backend && pytest tests -q

# Frontend
cd frontend && npx tsc --noEmit && npm run lint
```

## Demo accounts

Created by `docker compose exec backend python scripts/seed_demo_data.py`
(safe to re-run). Password for every account: `Demo123!`

| Role | Email |
|---|---|
| System admin | `admin@hms.demo` |
| Doctor | `doctor@hms.demo` |
| Nurse | `nurse@hms.demo` |
| Front desk | `frontdesk@hms.demo` |
| Lab tech | `labtech@hms.demo` |
| Pharmacist | `pharmacist@hms.demo` |
| Billing admin | `billing@hms.demo` |
| Patient (sign in at `/patient/login`, not `/login`) | `patient@hms.demo` |

## API documentation

Auto-generated OpenAPI/Swagger UI at `/docs` (ReDoc at `/redoc`) once the
backend is running — this is the live, authoritative API reference, kept in
sync with the code by construction (FastAPI generates it from the route
definitions and Pydantic schemas, it can't drift the way a hand-written doc
could).

## ER diagram

`docs/ER_DIAGRAM.md` — full 39-table schema diagram (Mermaid) plus
relationship notes, generated from and kept in sync with
`database/schema.sql`.

## Deployment

`docker-compose.yml` alone is for local/single-host dev (`docker compose up
-d --build`). `docker-compose.prod.yml` is a tested override for a shared
VPS that already runs [Traefik](https://traefik.io/) as its reverse proxy
(the pattern used for this app's own deployment):

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

It binds Postgres/Redis/RabbitMQ to `127.0.0.1` only (never exposed
publicly), and routes the frontend/backend through the host's existing
Traefik via Docker-provider labels instead of publishing their ports
directly — Traefik terminates TLS (Let's Encrypt) for
`HMS_FRONTEND_DOMAIN`/`HMS_API_DOMAIN` (set in `.env`, see
`.env.example`). Requires: `POSTGRES_PASSWORD`, `FRONTEND_ORIGIN` (the
`https://` frontend origin, for CORS), `HMS_FRONTEND_DOMAIN`,
`HMS_API_DOMAIN`, and a Traefik instance already running on that host with a
`letsencrypt` ACME cert resolver configured (this repo doesn't provision
Traefik itself, only the labels an existing one needs to pick the app up).

There is no CI/CD pipeline, Kubernetes manifests, or managed-cloud
deployment config in this repository; a fully managed production setup
(managed Postgres/Redis/RabbitMQ, secrets management, horizontal scaling of
the backend behind a load balancer, a real object-storage-backed static host
for the frontend build) is genuine infrastructure work beyond what's built
here.

## Known limitations

- **Google OAuth**: not implemented. `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`
  exist as config settings only — password-based JWT login is the only
  working sign-in method.
- **Payment gateway**: simulated, not real. "Mark paid" on an invoice is a
  staff-confirmed manual status flip — no Stripe/Razorpay/any processor is
  integrated. Tax/discount handling and PDF receipt generation are real.
- **SMS/email**: a documented logging stub (`backend/app/core/notification_providers.py`),
  not a real Twilio/SMTP integration — the stub logs what would be sent and
  includes a written guide for swapping in a real provider. In-app
  notification delivery status tracking and retry are real.
- **No real-time queue view for patients** — the live queue board is
  staff-only today.
- **No slot-grid picker in the booking UI yet** — the `available-slots` API
  exists and is tested (both staff and patient-facing), but neither
  `BookMyAppointmentPage.tsx` nor a staff booking form calls it yet; booking
  today is "enter a candidate time, get a clean 409 if it conflicts."
- **No dedicated staff appointment-list/reschedule page** — the reschedule
  API (`PATCH /appointments/{id}/reschedule`) exists and is tested, but has
  no frontend consumer yet.
- **No medical scans/file-attachment capability** anywhere in the system.
- **Test/dev database isolation**: the backend test suite runs against its
  own `hms_test` database (a separate database on the same Postgres
  instance, auto-created for a fresh `docker compose up` via
  `database/init_test_db.sh` — see `backend/tests/conftest.py`'s module
  docstring). Running `pytest` no longer touches `hms`, the database
  `docker-compose.yml`'s backend service and your seeded demo data actually
  use. On a Postgres data volume that predates this fix, create `hms_test`
  once by hand:
  ```bash
  docker compose exec postgres sh -c "psql -U hms -d postgres -c 'CREATE DATABASE hms_test;'"
  docker compose exec postgres sh -c "psql -U hms -d hms_test -f /docker-entrypoint-initdb.d/01-schema.sql"
  ```
  Redis is isolated the same way (tests use logical DB `/1`, the app uses
  `/0` — same server, different index).

## Demo video

Not recorded in this environment. `docs/DEMO_SCRIPT.md` has the full
walkthrough script (patient login → booking → check-in/token → consultation
→ prescription → lab → pharmacy → billing → admin dashboard) a presenter
would follow to record one.

## Presentation

Not built as slides in this environment. `docs/PRESENTATION.md` has the
full per-slide source content (problem, solution, users, modules, stack,
architecture, double-booking prevention, demo flow, test results, security,
future scope) ready to drop into a deck.
