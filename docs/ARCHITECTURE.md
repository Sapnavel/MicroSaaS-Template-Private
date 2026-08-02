# Hospital Management & Appointment Booking System — Architecture

> Multi-tenant, real-time, HIPAA/GDPR-aware HMS. This document covers the system design;
> `database/schema.sql` has the DDL; `backend/app/services/scheduling_engine.py` and
> `emergency_engine.py` have the concrete concurrency-critical engine code.

---

## 1. Scope of this delivery

Building all 10 modules to full production depth (billing/insurance state machines, ML
no-show scoring, lab/pharmacy inventory, OT scheduling, notification fan-out, dashboards)
is a multi-month effort. What's implemented **now**, at production-grade depth:

- Full relational schema for all 10 modules (`database/schema.sql`)
- Multi-tenancy, RBAC/ABAC, JWT auth, PHI field-level encryption, immutable audit log (foundation)
- **Atomic tri-resource booking engine** (doctor + room + equipment) with distributed
  locking + DB-level exclusion constraints — the hardest correctness problem in the system
- **Emergency preemption algorithm** (Triage 1/2 auto-bumps + reschedule pipeline)
- Live queue/token WebSocket broadcast
- Docker Compose infra: Postgres, Redis, RabbitMQ, backend, frontend

Everything else (lab workflow, pharmacy FEFO inventory, ward/bed matrix, billing/insurance
claims, notification hub, exec dashboards) is **scaffolded**: schema exists, router files
exist with typed stubs and TODOs, so each module can be built out module-by-module using
this repo's existing `/generate-prp` → `/execute-prp` workflow without re-architecting
the foundation.

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
| Auth | JWT (access+refresh) + Google OAuth | Matches CLAUDE.md |
| PHI encryption | `cryptography` Fernet, SQLAlchemy `TypeDecorator` | Field-level, transparent at the ORM layer — encrypted at rest, decrypted only in-process for authorized reads |
| Multi-branch sync | Postgres logical replication (cross-branch read replicas) + RabbitMQ event stream (cross-branch actions: transfers, emergency lookups) | Logical replication handles bulk data mirroring; the event bus handles low-latency cross-branch operational events (e.g. "Branch B needs to know Patient X was just triaged Level 1 at Branch A") |

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
5. A consumer (`notification_service`, stubbed) listens for `appointment.preempted` and
   runs the rebooking offer flow (next available slot, SMS/push notice) — decoupled from
   the hot path so the ER doesn't wait on notification delivery.

---

## 7. Real-time queue

Arrival check-in issues a `queue_tokens` row (`status: waiting`). Status transitions
(`in_consultation`, `delayed`, `skipped`, `done`) are pushed over a WebSocket topic scoped
per `(branch_id, department_id)`. Consultation-duration overruns trigger a recalculation of
downstream estimated wait times (simple weighted-moving-average per doctor to start;
swap-in point for the ML no-show/duration model later) and rebroadcast.

---

## 8. Project structure

```
backend/
├── app/
│   ├── main.py                  # FastAPI app, router registration, WS mount
│   ├── config.py                # Pydantic Settings (env-driven)
│   ├── database.py              # engine, SessionLocal, get_db
│   ├── core/
│   │   ├── security.py          # JWT, get_current_user, RBAC/ABAC Policy engine
│   │   ├── encryption.py        # PHI field-level encryption (TypeDecorator)
│   │   ├── locking.py           # Redis Redlock distributed lock
│   │   └── events.py            # RabbitMQ publisher/consumer interface
│   ├── models/                  # SQLAlchemy models (all 10 modules)
│   ├── schemas/                 # Pydantic request/response schemas
│   ├── services/
│   │   ├── scheduling_engine.py # Atomic Resource Booking Engine (implemented)
│   │   ├── emergency_engine.py  # Emergency Preemption Algorithm (implemented)
│   │   └── notification_service.py  # stub
│   ├── routers/                 # appointments, emergency implemented; rest stubbed
│   └── websocket/
│       └── queue_board.py       # live queue broadcast
├── alembic/
├── requirements.txt
└── Dockerfile

frontend/
└── src/{components,pages,hooks,services,context,types}/  # Vite skeleton, per CLAUDE.md conventions

database/
└── schema.sql                   # full DDL, all modules

docs/
└── ARCHITECTURE.md              # this file

docker-compose.yml               # postgres, redis, rabbitmq, backend, frontend
.env.example
```

---

## 9. Build-out order (recommended next steps)

Each module below can be built the same way this foundation was: define it in `INITIAL.md`
module sections, then implement against the existing schema. Suggested order, by
dependency and clinical-risk priority:

1. Auth/RBAC endpoints (wire the stubbed `core/security.py` into `routers/auth.py`)
2. Patient Master Index + dedup/merge workflow
3. Clinical consultation + prescription engine (depends on patient + appointment)
4. Lab + pharmacy (depends on consultation)
5. Ward/bed/OT (independent; can parallel with 3-4)
6. Billing/insurance (depends on 3-5 for chargeable events)
7. Notification hub (cross-cutting; wire consumers as each module starts publishing events)
8. Executive dashboard (depends on all of the above existing)
