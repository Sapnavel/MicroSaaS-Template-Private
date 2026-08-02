# PRP: Notification & Alert Hub

> Implementation blueprint for parallel agent execution.
> Module PRP inside the existing Hospital Management & Appointment Booking
> System scaffold — builds on `docs/ARCHITECTURE.md`, `database/schema.sql`,
> and `backend/app/core/events.py` (the publisher side, already built and
> already used by two prior modules). This is the consumer side.

---

## METADATA

| Field | Value |
|-------|-------|
| **Product** | Hospital Management & Appointment Booking System |
| **Module** | Notification & Alert Hub |
| **Version** | 1.0 |
| **Created** | 2026-07-31 |
| **Complexity** | Medium — no distributed-locking concurrency family this time; the real complexity is architectural shape: this is the first module in this codebase that is NOT a request/response engine. It's a long-running background consumer process (a second Docker service, not a new FastAPI router doing the real work) that reacts to events already flowing through RabbitMQ. |

---

## MODULE OVERVIEW

**Description:** Consumes the events already published to RabbitMQ by
`core/events.py`'s `event_publisher`, turns each into a `notifications` row,
and "sends" it through a pluggable per-channel provider (SMS/email/push).
Also exposes a small staff-facing HTTP surface to view notification history
and manually retry a failed send.

**Why this module, why now:** `docs/ARCHITECTURE.md` §9 lists it as
"cross-cutting; wire consumers as each module starts publishing events" —
the publisher side (`core/events.py`) and two real publishers
(`scheduling_engine.py`, `emergency_engine.py`) already exist and have been
live since the foundation scaffold.

**SCOPE-DEFINING FACT, read this before writing anything:** as of this PRP,
exactly **four** event topics are ever actually published anywhere in this
codebase — grep for `event_publisher.publish(` and you will find only these:

| Topic | Published by | Payload |
|---|---|---|
| `appointment.booked` | `scheduling_engine.book_appointment`, `emergency_engine` (emergency path) | `appointment_id, branch_id, doctor_id, patient_id, start_time, is_emergency` |
| `appointment.cancelled` | `scheduling_engine.cancel_appointment` (or similarly-named function — confirm exact name/signature by reading the file) | `appointment_id, reason` (no `patient_id`/`branch_id` — must be resolved via a DB lookup on `appointment_id`) |
| `queue.wait_time_updated` | `scheduling_engine.recalculate_downstream_wait_times` | `appointment_id, doctor_id` (no `patient_id`/`branch_id` — same resolution need) |
| `appointment.preempted` | `emergency_engine` | `victim_appointment_id, victim_patient_id, preempted_by_appointment_id, doctor_id, triage_level` (no `branch_id` — resolve via `victim_appointment_id`) |

The router stub's own TODO comment optimistically lists `lab.result_ready`
and `billing.receipt_issued` as example future topics — but Lab, Pharmacy,
Ward, and Billing (all four already built, reviewed, and shipped in prior
PRPs) **never call `event_publisher.publish(...)` anywhere**. Retrofitting
publish calls into four already-completed, already-reviewed modules is
**explicitly out of scope for this PRP** — that would mean reopening and
re-testing four settled modules for a change none of their own Phase 3
reviews flagged as missing. This PRP's job, matching the architecture
doc's own phrasing ("wire consumers **as** each module starts publishing
events" — present/future tense, not "go back and retrofit"), is to build
the consumer machinery generically enough that adding a fifth topic later
(when/if a future module starts publishing one) is a one-entry addition to
a registry, not an architecture change. Say this plainly in code comments
— it is a real, deliberate scope boundary, not an oversight.

**What already exists (do not recreate):**
- `database/schema.sql` §12 — `notifications` (user_id, patient_id, channel,
  template, payload JSONB, status, created_at). This PRP adds a nullable
  `branch_id` column (see DATA MODEL below) — the table has never been
  used (zero ORM model, zero rows), so this is a fresh additive change, the
  same kind of first-use refinement every prior module's Phase 1 has made
  to its own scaffolded DDL (Ward added RLS, Billing added CHECK
  constraints — none of these were "wrong" before, just not yet needed).
- `backend/app/core/events.py` — `EventPublisher` Protocol,
  `RabbitMQPublisher` (topic exchange `hms.events`, durable, topic-typed).
  **Reuse this exchange directly** — do not declare a second one.
- `backend/app/config.py` — `rabbitmq_url`, `events_exchange` settings,
  already used by `RabbitMQPublisher`.
- `docker-compose.yml` — `rabbitmq` service (management image, healthcheck)
  already running; `backend` service already depends on it. This PRP adds
  a **second** service (the consumer worker — see PHASE EXECUTION PLAN)
  since a long-running `pika` blocking consumer cannot share a process
  with the FastAPI app's async event loop without real complexity FastAPI
  itself doesn't need here — a separate container is the honest shape.
- `backend/app/models/patient.py` — `Patient.phone` (`EncryptedString`,
  decrypts transparently on ORM read) — the SMS "send" step reads this.
- `backend/app/models/user.py` — `User.email` — the email "send" step
  reads this.
- `backend/app/websocket/queue_board.py` — the LIVE, synchronous queue
  broadcast (already built, foundation scaffold). This module is
  deliberately **not** a replacement or duplicate of that — the websocket
  board is for in-app live status; this hub is for async, out-of-band
  channels (SMS/email/push) a patient receives outside the app. Don't
  merge the two; they solve different problems.

**Key design decisions this PRP makes (read before implementing):**

1. **A stub `NotificationProvider` per channel, not a real Twilio/SendGrid/FCM
   integration.** No provider credentials exist in this scaffold's env vars.
   `LoggingNotificationProvider` (one class, reused for `sms`/`email`/`push`)
   logs what it would have sent and always "succeeds" — the same
   "swap-in point, not a real integration" spirit `core/events.py`'s own
   docstring already uses for RabbitMQ-vs-Kafka. Structure the `Protocol`
   so a real provider is a drop-in later, not an architecture change.

2. **No retry/backoff queue or dead-letter table.** The router stub's TODO
   mentions this; building a real backoff scheduler is a substantial
   feature in its own right this PRP does not attempt. Instead: a failed
   send sets `notifications.status = "failed"` and logs the reason: a
   staff member can manually retry via `PATCH /notifications/{id}/retry`
   (which just re-attempts the same provider call). This is the same
   "manual action over inventing automation" scope cut Billing made for
   `mark_invoice_paid` instead of a real payment-gateway webhook.

3. **The consumer's per-topic handlers are plain, unit-testable functions —
   the `pika` consume loop is a thin wrapper around them.** `notification_engine.py`
   exposes `handle_event(db: Session, topic: str, payload: dict) -> Notification`
   as the single entry point; the actual `pika` `BlockingConnection`/
   `basic_consume` loop lives in `workers/notification_consumer.py` and does
   nothing but decode JSON and call `handle_event`, ack on success, log +
   nack (no requeue — see design decision #2, there's no DLQ to requeue
   into) on failure. This mirrors why `ward_engine.py`'s pure-vs-locking
   split exists: keep the part worth unit-testing free of the part that
   needs a real broker connection to exercise at all.

4. **`branch_id` resolution.** All four current topics can resolve a
   `branch_id`: `appointment.booked` carries it directly; the other three
   only carry `appointment_id` (or `victim_appointment_id`), so
   `handle_event` looks up the `Appointment` row for those and pulls
   `branch_id`/`patient_id` off of it. If a future topic genuinely cannot
   resolve one, `Notification.branch_id` stays nullable (nullable in the
   ORM despite every current handler always populating it) — a `NULL`
   value is not an error state, just an honest "not resolvable this time."

**MVP Scope:**
- [ ] ORM model for `Notification` (+ `branch_id` schema addition).
- [ ] `NotificationProvider` Protocol + `LoggingNotificationProvider` stub,
  one per channel (`sms`, `email`, `push`), registry keyed by channel.
- [ ] `notification_engine.handle_event(db, topic, payload) -> Notification`
  — topic → (recipient resolution, channel, template) registry covering
  exactly the four topics listed above.
- [ ] `workers/notification_consumer.py` — the actual `pika` consumer loop,
  runnable as its own process/Docker service.
- [ ] `GET /api/v1/notifications` (history, filterable), `PATCH
  /api/v1/notifications/{id}/retry` (manual resend of a `failed` row).
- [ ] Frontend: a simple staff-facing notification history page.

---

## TECH STACK

| Layer | Technology |
|-------|------------|
| Backend | FastAPI (HTTP surface only) + `pika` (consumer worker, separate process) + SQLAlchemy |
| Frontend | React + TypeScript |

---

## DATA MODEL

### Schema change (apply first)
`database/schema.sql` §12 `notifications`: add `branch_id UUID REFERENCES
branches(id)` (nullable — see design decision #4), plus CHECK constraints
`channel IN ('sms','email','push')` and `status IN ('queued','sent','failed')`
(the same "cheap DB-level backstop on a plain-TEXT lifecycle column"
convention Billing's PRP established for `invoices.status`). §13 RLS: add a
`branch_isolation` policy on `notifications` using the new `branch_id`
column directly (real column this time, no join needed) — but see the
ENDPOINTS section for why a `NULL` branch_id needs an explicit `OR
branch_id IS NULL` clause or equivalent so system_admin/legitimately-
unresolvable rows aren't invisible to everyone.

### ORM model (`backend/app/models/notification.py`, new file)
- `NotificationChannel(str, enum.Enum)`: `sms, email, push`. Plain `String`
  column (not a Postgres enum), matching the CHECK-constraint convention.
- `NotificationStatus(str, enum.Enum)`: `queued, sent, failed`. Same.
- `Notification`: `user_id` (FK users.id, nullable), `patient_id` (FK
  patients.id, nullable), `branch_id` (FK branches.id, nullable — new
  column), `channel` (String), `template` (String), `payload` (JSONB dict),
  `status` (String, default `"queued"`), `created_at`.

---

## ENGINE DESIGN

### `backend/app/core/notification_providers.py`
```python
class NotificationProvider(Protocol):
    def send(self, *, channel: str, recipient: str, template: str, payload: dict) -> bool: ...

class LoggingNotificationProvider:
    def send(self, *, channel, recipient, template, payload) -> bool:
        logger.info("(stub) would send %s via %s to %s: %s", template, channel, recipient, payload)
        return True

_PROVIDERS: dict[str, NotificationProvider] = {
    "sms": LoggingNotificationProvider(), "email": LoggingNotificationProvider(), "push": LoggingNotificationProvider(),
}
```
`recipient` is a human-meaningful string the provider would actually use
(a phone number for `sms`, an email address for `email`) — resolved by
`notification_engine.py` from `Patient.phone`/`User.email` before calling
`send`, not inside the provider itself (the provider doesn't know how to
look up a patient).

### `backend/app/services/notification_engine.py`

`_TOPIC_HANDLERS: dict[str, Callable[[Session, dict], _ResolvedRecipient]]`
— one entry per topic, each resolving `(user_id, patient_id, branch_id,
channel, template)` from the raw payload dict (looking up `Appointment` by
id where the payload doesn't carry `patient_id`/`branch_id` directly, per
design decision #4). Example entries required:
- `appointment.booked` → channel `sms`, template `appointment_confirmation`,
  patient_id/branch_id straight off the payload.
- `appointment.cancelled` → look up `Appointment` by `appointment_id` for
  `patient_id`/`branch_id`; channel `sms`, template `appointment_cancelled`.
- `appointment.preempted` → channel `sms`, template
  `appointment_preempted_rebooking_offer` (per `docs/ARCHITECTURE.md` §6
  point 5's own wording — this is the exact consumer that section
  describes as "stubbed"); `patient_id` straight off the payload
  (`victim_patient_id`), `branch_id` via `Appointment` lookup on
  `victim_appointment_id`.
- `queue.wait_time_updated` → look up `Appointment` by `appointment_id` for
  `patient_id`/`branch_id`; channel `push`, template `wait_time_update`.

`handle_event(db, *, topic, payload) -> Notification | None` (returns
`None` and logs a warning for an unrecognized topic — this is not an error,
just nothing to do yet, per the scope note above): resolves the recipient
via `_TOPIC_HANDLERS[topic]`, resolves the actual `recipient` string
(`Patient.phone` if `patient_id` set, else `User.email` if `user_id` set),
creates a `Notification(status="queued")`, commits (so the row exists even
if sending then fails), calls the channel's provider, sets `status =
"sent"` or `"failed"` based on the result (catch any exception from the
provider — a send failure must never crash the consumer loop or leave the
row in `"queued"` forever), commits again, and returns the row.

### `backend/app/workers/notification_consumer.py`
Standalone script (`if __name__ == "__main__":` entry point, runnable as
`python -m app.workers.notification_consumer`). Declares the SAME topic
exchange `core/events.py` already declares (`hms.events`, topic, durable),
declares its own durable queue bound to exactly the four topic routing keys
in `_TOPIC_HANDLERS` (not a wildcard `#` — an unrecognized topic should be
a deliberate no-op inside `handle_event`, not silently consumed by an
overly-broad binding that could swallow future topics meant for a
different consumer), consumes with manual ack, opens its own DB session
per message (mirrors the FastAPI app's `get_db` pattern, but this process
has no request scope to hang a session off of), calls
`notification_engine.handle_event`, acks on success, logs + nacks
(`requeue=False`, since there's no DLQ — see design decision #2) on any
exception so a single bad message can't wedge the consumer forever.

---

## ENDPOINTS

| Method | Endpoint | Auth | Description |
|--------|----------|------|--------------|
| GET | /api/v1/notifications | front_desk, system_admin | Query params `patient_id`/`user_id`/`status`/`branch_id` (branch_id required for non-system_admin, same rule as every prior branch-scoped list endpoint). |
| PATCH | /api/v1/notifications/{id}/retry | system_admin | Re-attempts the provider send for a `status="failed"` row; 409 if not currently `failed`. |

No role beyond `front_desk`/`system_admin` gets any access — this is an
operational/support surface, not a clinical one; no `doctor`/`nurse`/
`billing_admin`/etc. access needed, matching the "no access at all for
roles with no legitimate reason" discipline every prior module's
ENDPOINTS table has used.

---

## FILES TO CREATE / MODIFY

**Create:**
- `backend/app/models/notification.py` — `NotificationChannel`,
  `NotificationStatus`, `Notification`.
- `backend/app/core/notification_providers.py` — `NotificationProvider`,
  `LoggingNotificationProvider`, `_PROVIDERS` registry.
- `backend/app/services/notification_engine.py` — `handle_event` + the
  four topic handlers.
- `backend/app/schemas/notification.py` — request/response schemas.
- `backend/app/services/notification_service.py` — thin
  authorize()-wrapped service layer for the two HTTP endpoints (does NOT
  duplicate `notification_engine.handle_event` — that's the consumer's
  entry point, this is the HTTP-facing read/retry surface).
- `backend/app/workers/notification_consumer.py` (new `workers/` package
  — add `__init__.py`) — the `pika` consumer loop.
- `frontend/src/services/notificationService.ts`,
  `frontend/src/pages/NotificationHistoryPage.tsx`.
- `backend/tests/test_notification_engine.py`,
  `backend/tests/test_notifications_router.py`.

**Modify:**
- `database/schema.sql` — §12 `branch_id` column + CHECK constraints, §13 RLS.
- `backend/app/models/__init__.py` — register new models.
- `backend/app/routers/notifications.py` — replace the stub.
- `backend/app/core/security.py` — register `notification` resource_type
  policies for `front_desk` (read), `system_admin` covered by
  `_admin_bypass`.
- `backend/app/main.py` — no change expected (router already included).
- `docker-compose.yml` — add a `notification_worker` service (same build
  context as `backend`, `command: python -m app.workers.notification_consumer`,
  same env vars, `depends_on: rabbitmq (healthy), postgres (healthy)`).
- `frontend/src/App.tsx` — register the history page route.

---

## PHASE EXECUTION PLAN

**Phase 1: Models + provider abstraction + engine (sequential)**
- BACKEND-AGENT: confirm schema.sql changes (branch_id, CHECK, RLS) applied
  and match this PRP, then `models/notification.py`,
  `core/notification_providers.py`, `services/notification_engine.py`.

**Validation Gate 1:** models import cleanly against a real Postgres; a
live round-trip proves `handle_event` correctly resolves recipient/branch
for all four topics (construct a real `Appointment` fixture chain and feed
`handle_event` a payload shaped exactly like what `scheduling_engine.py`/
`emergency_engine.py` actually publish — read those two files' exact
`event_publisher.publish(...)` call sites to get the payload shape
byte-for-byte right, don't guess); an unrecognized topic returns `None`
without raising.

**Phase 2: Endpoints + worker + frontend (parallel)**
- BACKEND-AGENT: `schemas/notification.py`, `services/notification_service.py`,
  `routers/notifications.py`, `workers/notification_consumer.py`,
  `core/security.py` policy registration, `docker-compose.yml` worker
  service.
- FRONTEND-AGENT: notification history page.

**Validation Gate 2:** `ruff check backend/`, `mypy backend/app`, `npm run lint`, `tsc --noEmit`; `docker compose config` validates the new service definition.

**Phase 3: Quality (parallel)**
- TEST-AGENT: unit tests for every `_TOPIC_HANDLERS` entry (all four
  topics, with realistic payload shapes copied from the actual publish
  call sites); `handle_event` returns `None` for an unrecognized topic;
  a provider failure (mock `LoggingNotificationProvider.send` to raise)
  results in `status="failed"`, not a crash, and the row still persists;
  `PATCH /retry` succeeds on a `failed` row and 409s on a `queued`/`sent`
  one; RBAC (front_desk read-only, system_admin full, no other role has
  any access); branch tenant isolation on the GET history endpoint. If
  feasible, an actual end-to-end test that runs the consumer against the
  real RabbitMQ container for one message (publish via
  `event_publisher.publish` directly, then run the consumer's loop body
  for a bounded number of messages/timeout and confirm a `Notification`
  row appears) — if this proves too flaky/slow for the test suite, unit
  tests on `handle_event` plus a review of the consumer script's
  ack/nack logic are an acceptable substitute; say explicitly which you
  did and why.
- REVIEW-AGENT: does `handle_event` ever let a provider exception escape
  uncaught (would crash the consumer loop)? Is the consumer's queue
  binding scoped to exactly the four known topics, not a wildcard that
  could silently swallow a future module's differently-intended topic? Is
  `branch_id` resolution actually correct for all three topics that need
  an `Appointment` lookup (not just assumed present in the payload)? Does
  `PATCH /retry` correctly gate on `status == "failed"` only? Branch
  isolation on `GET /notifications` — does a non-system_admin caller
  omitting/mismatching `branch_id` get a 422, and does the RLS policy
  correctly handle rows where `branch_id IS NULL` (should system_admin
  only, or is there a legitimate non-admin reason to see one — make a
  call and document it)?

---

## VALIDATION GATES

| Gate | Commands |
|------|----------|
| 1 | Apply schema.sql to fresh Postgres; import new models; live `handle_event` round-trip for all four topics against real Appointment fixtures |
| 2 | `ruff check backend/`, `mypy backend/app`, `npm run lint`, `tsc --noEmit`, `docker compose config` |
| 3 | `pytest backend/tests/test_notification_engine.py backend/tests/test_notifications_router.py --cov=app.services.notification_engine --cov=app.services.notification_service --cov=app.routers.notifications --cov-fail-under=80` |

---

## NEXT STEP

```
/execute-prp PRPs/notification-hub-prp.md
```
