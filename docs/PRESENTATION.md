# Presentation Content

HMS Project Completion Prompt deliverable (section 6.9): source content for
a slide deck. No slide file exists yet — this is the per-slide script; drop
each `##` section into one slide.

---

## 1. Problem

Hospitals running on paper registers or disconnected point tools lose track
of the same thing in five different ways: a doctor double-booked across two
systems, a bed shown "free" that's actually occupied, a prescription written
without knowing the patient is allergic to it, a bill missing half the
charges because nobody reconciled the lab report against it. Each module
individually is solvable; the actual hard problem is keeping all of them
*consistent* under concurrent, real-world use — two front-desk clerks
booking the same doctor's last slot at the same second, an admission and a
transfer racing for the same bed.

## 2. Proposed solution

A single hospital management system where every module (scheduling, queue,
consultations, lab, pharmacy, wards/OT, billing, notifications) shares one
tenant-scoped database and one authorization model, with conflict-prone
operations (booking, bed allocation, stock deduction) enforced at the
**database** level via exclusion constraints and row locks — not just
application-level checks that can race.

## 3. Main users

| Role | Primary jobs-to-be-done |
|---|---|
| Patient | Find a doctor, book/track appointments, view own records/bills/lab results |
| Front desk | Register patients, check in, manage the queue, view billing |
| Doctor | Search/book, run consultations, prescribe, order labs, view queue |
| Nurse | Queue, lab worklist, bed matrix, OT schedule |
| Lab tech | Process orders through the collection → verification workflow |
| Pharmacist | Dispense against stock, manage inventory/batches |
| Billing admin | Invoice, split with insurers, track claims, collect payment |
| System admin | Cross-branch visibility, executive dashboard, full access |

## 4. Important modules

Patient Master Index (dedup + safe merge) · Appointment Scheduling
(doctor+room+equipment, emergency preemption, waitlist) · Real-Time Queue &
Token · Consultation & Prescription (allergy/interaction safety engine) ·
Laboratory (order → sample → result state machine) · Pharmacy (FEFO
dispense, batch/expiry tracking) · Ward/Bed/OT management · Billing &
Insurance Claims (now with tax/discount + generated PDF receipts) ·
Notification Hub (RabbitMQ-backed, retryable) · Executive Dashboard.

## 5. Technology stack

- **Backend:** FastAPI (Python 3.11+), SQLAlchemy 2.0
- **Database:** PostgreSQL 15+ (`btree_gist` for exclusion constraints,
  `pg_trgm` for fuzzy patient-name matching)
- **Frontend:** React + TypeScript + Vite
- **Async infrastructure:** Redis (distributed locks), RabbitMQ (event
  bus + notification/queue-wait-time workers)
- **Auth:** JWT (rotating signing keys, Redis-backed revocation), bcrypt
- **Containerization:** Docker Compose (7 services: backend, frontend,
  postgres, redis, rabbitmq, notification worker, queue-wait-time worker)

## 6. Architecture

```
Browser (React SPA)
    |
    v
FastAPI backend  --auth-->  JWT + role/branch claims
    |         \
    |          \--> Redis (distributed locks, rate limits, token revocation)
    |          \--> RabbitMQ (event bus) --> notification worker
    |                                    \--> queue-wait-time worker
    v
PostgreSQL (single tenant-scoped schema, RLS-adjacent branch scoping,
            exclusion constraints on every conflict-prone table)
```

Every request is role-gated server-side (`require_role`) and, for anything
resource-scoped, tenant-gated (`authorize()` against the caller's branch) —
hiding a button in the UI is never the actual security boundary.

## 7. Double-booking prevention

Three independent layers, in order of authority:

1. **Database exclusion constraint** (`EXCLUDE USING gist`) on
   `(doctor_id, time_range)`, `(room_id, time_range)`,
   `(equipment_id, time_range)`, `(bed_id, stay_range)` — this is what
   actually can't be raced around; a second concurrent transaction gets a
   Postgres `IntegrityError`, not a maybe.
2. **Row lock** (`SELECT ... FOR UPDATE`) on the pre-check, so the common
   case fails fast with a clear 409 instead of always falling through to
   the constraint.
3. **Single atomic transaction** — doctor, room, and equipment are reserved
   together or not at all; there's no window where a partial booking exists.

Verified by an automated test that fires 8 concurrent HTTP requests at the
same slot and asserts exactly one `201` and one non-cancelled row in the
database.

## 8. Demonstration flow

See `docs/DEMO_SCRIPT.md` — patient search → booking → check-in/token →
consultation → prescription (with a live safety-engine warning) → lab
order → dispensing (FEFO) → invoice with tax/discount → PDF receipt →
admin dashboard, all against the real running system.

## 9. Test results

642 backend tests passing (unit + integration, run against a real
Postgres/Redis/RabbitMQ, no mocks), including a dedicated concurrency test
proving exactly-one-success under simultaneous booking attempts. See
`README.md`'s "Test commands" section to reproduce.

## 10. Security

bcrypt password hashing · JWT with key rotation and Redis-backed revocation
· server-side RBAC + branch-tenant authorization on every protected
endpoint · hash-chained, tamper-evident audit log for sensitive actions ·
Pydantic input validation + SQLAlchemy ORM throughout (no raw SQL
injection surface) · PHI fields (clinical notes, lab results) encrypted at
rest · scoped CORS (no wildcard origin) · security headers (CSP, X-Frame-
Options, HSTS, etc. on every response) · rate limiting on login/
patient-search (tight, endpoint-specific) plus a coarse global limit on
every mutating request (broad, defense-in-depth). Remaining gap: no
file-upload capability exists anywhere in the system (so nothing to
validate there yet either) — see "Future scope" below.

## 11. Future scope

Real payment gateway integration (current flow is an explicitly-simulated
manual status flip) · real SMS/email provider (currently a documented
logging stub) · Google OAuth login (config settings exist, no flow is
wired up — password-based JWT login is the only working method today) ·
medical scans/file attachments · insurance policy records
(currently claim-level only) · a slot-grid picker in the booking UI (the
`available-slots` API exists and is tested, but no frontend page calls it
yet) · a dedicated staff appointment-list/reschedule page (the reschedule
API exists and is tested, but has no frontend consumer yet) ·
patient-facing live queue view (the queue board is staff-only today) ·
appointment reminders (booking/cancellation notifications exist; a
pre-visit reminder does not, since there's no scheduler/cron
infrastructure to fire one off a time delta yet).
