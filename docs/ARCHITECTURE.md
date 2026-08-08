# Hospital Management & Appointment Booking System — Architecture

> Multi-tenant, real-time, HIPAA/GDPR-aware HMS. This document covers the system design;
> `database/schema.sql` has the DDL (`docs/ER_DIAGRAM.md` has the diagram);
> `backend/app/services/scheduling_engine.py` and `emergency_engine.py` have the concrete
> concurrency-critical engine code.

---

## 1. Scope of this delivery

All 10 modules are implemented end-to-end (schema → service → router → frontend page),
not scaffolded — confirmed by a 642-test backend suite exercising real business logic
against a real Postgres/Redis/RabbitMQ stack, not mocks. See `docs/ER_DIAGRAM.md` for the
full schema (39 tables) and `README.md` for how to run it.

- Full relational schema for all 10 modules (`database/schema.sql`)
- Multi-tenancy, RBAC/ABAC, JWT auth (rotating signing keys, Redis-backed revocation), PHI
  field-level encryption, immutable hash-chained audit log
- **Atomic tri-resource booking engine** (doctor + room + equipment) with distributed
  locking + DB-level exclusion constraints — the hardest correctness problem in the system
  — plus rescheduling and real available-slot computation on top of it
- **Emergency preemption algorithm** (Triage 1/2 auto-bumps + reschedule pipeline)
- Real-time queue/token WebSocket broadcast, with emergency/priority queue-jumping
- Patient Master Index with deterministic + fuzzy (trigram) duplicate detection and a safe
  merge workflow
- Clinical consultation + prescription flow with a real drug-allergy/interaction safety
  engine (DB-backed, tiered BLOCK/OVERRIDE_REQUIRED/INFO findings)
- Lab order lifecycle (ordered → collected → processing → verified → attached)
- Pharmacy FEFO (first-expired-first-out) dispensing with atomic stock deduction
- Ward/bed/OT management with the same exclusion-constraint discipline as appointments
- Billing: itemized invoices, tax/discount, insurance claim state machine, generated PDF
  receipts (payment collection itself is a staff-confirmed manual status flip — see
  `docs/PRESENTATION.md` section 10 for exactly what "simulated" means here)
- Notification hub (RabbitMQ-backed, retryable) — SMS/email providers are a documented
  logging stub pending real credentials, not a fake integration
- Executive dashboard with real SQL aggregates (occupancy, wait times, revenue, no-show
  rate, stock alerts)
- A unified, cross-module patient timeline (appointments/consultations/labs/prescriptions/
  admissions/invoices merged and sorted)

Known, deliberately out-of-scope gaps (not silently missing — each is a documented
decision): real payment gateway integration, real SMS/email provider credentials, medical
scans/file uploads, a dedicated staff-facing appointment-reschedule UI (the API exists).
See `docs/PRESENTATION.md` section 11 for the full future-scope list.

---

## 2. Tech stack

| Layer | Choice | Why |
|---|---|---|
| Backend | FastAPI + Python 3.11+ (async) | Matches project CLAUDE.md; async fits WebSocket/SSE + I/O-bound queue work |
| DB | PostgreSQL 15+ | `tstzrange` + `EXCLUDE USING gist` gives us **DB-enforced** double-booking prevention, not just app-level locking. JSONB for flexible ICD-10 payloads. Row-Level Security available for tenant isolation. |
| ORM/Migrations | SQLAlchemy 2.0 + Alembic | Matches CLAUDE.md |
| Distributed lock | Redis (Redlock algorithm) | Coordinates booking attempts *across API processes/pods* before they even reach the DB, so we fail fast under contention instead of stacking up DB row locks. Real Redlock needs ≥3 independent Redis masters in prod; scaffold here documents single-instance dev mode. |
| Event bus | RabbitMQ (Kafka-compatible topology) | Async fan-out for: reschedule pipeline, notifications, multi-branch sync, audit stream. RabbitMQ chosen over Kafka for lower ops overhead at hospital-network scale (10s–100s of branches, not internet scale). Swap-in Kafka is straightforward since the engine only depends on a thin `EventPublisher` interface. |
| Realtime | WebSockets (queue board) + SSE fallback | Live token/queue status per department |
| Auth | JWT (access+refresh), password-based | JWT is fully implemented (rotating signing keys, Redis-backed revocation). Google OAuth is **not** — `google_client_id`/`google_client_secret` exist as unused config settings only, no OAuth flow endpoint exists. Listed here as a documented gap, not overstated as built. |
| PHI encryption | `cryptography` Fernet, SQLAlchemy `TypeDecorator` | Field-level, transparent at the ORM layer — encrypted at rest, decrypted only in-process for authorized reads |
| Multi-branch data model | Single shared Postgres database, `branch_id` row-level tenancy | Every branch reads/writes the same database (see section 3) — this is what's actually built. Postgres logical replication for read-replica scaling and a dedicated low-latency cross-branch event stream (beyond the general RabbitMQ event bus already in place) are noted here as a plausible scaling path, not implemented infrastructure. |

---

## 3. Multi-tenancy model

Row-level tenancy: every clinical/operational table carries `branch_id` (branch = tenant
unit; branches roll up to a `hospital_group_id` for cross-branch reporting). Enforced via:

1. Application-layer: every query is scoped through a `TenantContext` dependency injected
   from the JWT claims — there is no code path that queries without a branch filter.
2. Defense in depth: Postgres Row-Level Security policies (`database/schema.sql` includes
   the RLS policy template) so a bug in application code can't leak cross-branch PHI.

---

## 4. RBAC/ABAC

- **RBAC**: `roles`, `permissions`, `role_permissions` — coarse-grained ("can read lab orders").
- **ABAC layer on top**: permission checks additionally evaluate *attributes* of the
  request — e.g. a Doctor role can read `consultations` only where
  `consultations.doctor_id == current_user.doctor_id` OR the doctor is covering that
  patient's active admission; Front Desk can read patient demographics but not
  `clinical_notes` or `diagnoses`. This is implemented as a `Policy` callable per
  (role, resource, action) in `core/security.py`, not hardcoded per-endpoint, so new
  policies compose instead of forking endpoint logic.

---

## 5. The concurrency problem (why this is the hard part)

A booking touches **three independently-contended resources** — Doctor, Room, Equipment —
over a **time interval**, not a single row. Naive `SELECT` then `INSERT` races:

```
T1: check doctor free 10:00-10:30 → yes
T2: check doctor free 10:00-10:30 → yes   (T1 hasn't committed yet)
T1: insert booking
T2: insert booking   ← double-booked
```

We defend at **two independent layers**, deliberately redundant:

1. **Pre-DB distributed lock (Redis)**: before touching Postgres, the engine acquires a
   Redlock keyed on `(resource_type, resource_id, time_bucket)` for doctor, room, and
   every equipment id involved. This fails fast under contention (no DB connection burned
   waiting) and lets us give the caller "resource busy, retry" quickly. Locks are acquired
   in a **globally consistent sorted order** (sorted by resource key) to prevent
   lock-ordering deadlocks between concurrent multi-resource bookings.
2. **DB-level exclusion constraint (authoritative)**: `appointments` has
   `EXCLUDE USING gist (doctor_id WITH =, time_range WITH &&) WHERE (status != 'cancelled')`
   (and equivalent for room/equipment via a resource-slot junction table). This is the
   real correctness guarantee — even if the Redis lock is lost (node failure, network
   partition, TTL race), Postgres physically rejects the overlapping insert with a
   `23P01 exclusion_violation`, which the engine catches and converts into a clean
   "slot no longer available" response. Redis buys latency and throughput; Postgres buys
   correctness.

See `scheduling_engine.py` for the implementation.

---

## 6. Emergency preemption

Triage 1/2 cases don't wait for a free slot — they **take** the earliest matching slot and
push the bumped patient into an auto-reschedule pipeline. See
`emergency_engine.py` for the algorithm; summary:

1. Find candidate resource combos (doctor with matching specialty + on shift, room, required
   equipment) ordered by earliest availability.
2. If a truly free slot exists within the acceptable emergency window → book it, done.
3. Otherwise, select the **lowest-priority-cost victim appointment** among currently booked
   non-critical slots that would free up a valid combo soonest (cost = f(triage level of
   bumped patient, how close their appointment already is, how many times they've already
   been bumped — to avoid starving the same patient repeatedly).
4. Transactionally: cancel/park the victim appointment as `preempted`, insert the emergency
   booking, publish `appointment.preempted` event.
5. `notification_engine.py`'s `_handle_appointment_preempted` consumes the
   `appointment.preempted` event (via the `notification_worker` RabbitMQ consumer) and
   queues a rebooking-offer notification — decoupled from the hot path so the ER doesn't
   wait on notification delivery. The notification is genuinely queued and tracked
   (`queued`/`sent`/`failed` status, retryable); only the actual SMS/email *provider* is a
   documented logging stub pending real credentials (see `README.md`'s "Known limitations").

---

## 7. Real-time queue

Arrival check-in issues a `queue_tokens` row (`status: waiting`). Status transitions
(`in_consultation`, `delayed`, `skipped`, `done`) are pushed over a WebSocket topic scoped
per `(branch_id, department_id)`. Consultation-duration overruns publish a
`queue.wait_time_updated` event per downstream appointment; a dedicated
`queue_wait_time_worker` (RabbitMQ consumer, `backend/app/workers/queue_wait_time_consumer.py`)
recomputes and rebroadcasts each affected token's estimate, decoupled from the request path
that triggered it.

A token's `is_priority` flag (set explicitly at check-in, or auto-inherited from a booked
appointment's `is_emergency` flag) makes the live board sort priority-first, then
earliest-checked-in-first within each group — an emergency patient jumps the line without
losing fairness relative to other emergency patients who arrived earlier.

---

## 8. Project structure

```
backend/
├── app/
│   ├── main.py                  # FastAPI app, security-header + rate-limit middleware, router registration, WS mount
│   ├── config.py                # Pydantic Settings (env-driven)
│   ├── database.py              # engine, SessionLocal, get_db
│   ├── core/
│   │   ├── security.py          # JWT, get_current_user, RBAC/ABAC Policy engine
│   │   ├── encryption.py        # PHI field-level encryption (TypeDecorator)
│   │   ├── locking.py           # Redis Redlock distributed lock
│   │   ├── rate_limit.py        # fixed-window rate limiting (login, patient search, global write)
│   │   └── events.py            # RabbitMQ publisher/consumer interface
│   ├── models/                  # SQLAlchemy models, one file per module (13 files)
│   ├── schemas/                 # Pydantic request/response schemas
│   ├── services/                # business logic -- 26 files, one (or a small family) per module;
│   │                             # notable: scheduling_engine.py (booking/reschedule/available-slots),
│   │                             # emergency_engine.py (preemption), patient_timeline_service.py
│   ├── routers/                 # 17 routers -- thin: validation, role gating, status-code mapping only
│   ├── workers/                 # notification_consumer.py, queue_wait_time_consumer.py (RabbitMQ consumers)
│   └── websocket/
│       └── queue_board.py       # live queue broadcast
├── tests/                       # 642 tests (pytest, real Postgres+Redis+RabbitMQ, no mocks)
├── alembic/
├── requirements.txt
└── Dockerfile

frontend/
└── src/
    ├── pages/                   # one page per route, ~50 pages across all 10 modules
    ├── components/selects/      # reusable scoped dropdown/search-select components
    ├── services/                # one file per backend module; the ONLY place each knows the wire format
    ├── hooks/, context/, types/

database/
└── schema.sql                   # full DDL, all modules (authoritative -- see docs/ER_DIAGRAM.md)

docs/
├── ARCHITECTURE.md              # this file
├── ER_DIAGRAM.md                # full schema diagram + relationship notes
├── DEMO_SCRIPT.md               # walkthrough script for a recorded demo
└── PRESENTATION.md              # slide-deck source content

docker-compose.yml               # postgres, redis, rabbitmq, backend, frontend, notification_worker, queue_wait_time_worker
.env.example
```

---

## 9. Current module status

Every module in section 1's list is implemented and tested. There is no "build-out order"
left to plan — remaining work is either genuinely external (real payment/SMS/email
provider credentials) or a deliberately deferred UI (e.g. a dedicated staff appointment
list/reschedule page; the reschedule API itself exists and is tested). See
`docs/PRESENTATION.md` section 11 for the complete, current future-scope list.
