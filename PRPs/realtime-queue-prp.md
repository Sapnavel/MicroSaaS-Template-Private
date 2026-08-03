# PRP: Real-Time Queue & Digital Token Module

## METADATA

- **Module**: Real-Time Queue (`docs/ARCHITECTURE.md` §7 "Real-time queue"). NOT one of the 8 numbered items in §9's build-out order — that list only covers Foundation through Executive Dashboard, all of which are now built. This module is the one piece of infrastructure §1 ("Scope of this delivery") lists as already scaffolded (`QueueToken` model, `app/websocket/queue_board.py`'s WebSocket broadcast channel) but whose actual endpoints (`routers/queue.py`) were left a stub the whole session, referenced only as a documented gap by two already-shipped modules (Notification Hub's `_handle_wait_time_updated`, Executive Dashboard's `get_wait_times`).
- **Replaces**: `backend/app/routers/queue.py`'s existing STUB.
- **Adds no new tables/models**: `queue_tokens` (`models/queue.py`'s `QueueToken`/`TokenStatus`) already exists and is already correct for this module's needs — confirmed by reading it in full, not assumed.

## MODULE OVERVIEW — key design decisions

1. **`QueueToken` has no `patient_id` column of its own** — confirmed by reading `models/queue.py`: `branch_id`, `appointment_id` (nullable), `department_id` (nullable, FK `specialties.id`), `token_number`, `status`, `checked_in_at`, `called_at`, `completed_at`, `estimated_wait_minutes`. Patient identity is only reachable transitively via `appointment_id -> Appointment.patient_id`, when an appointment is linked. Check-in therefore supports two paths:
   - **Appointment-linked check-in** (the common case: a patient arrives for their booked appointment): caller supplies `appointment_id`; `branch_id`/`department_id` are derived from the appointment (`Appointment.branch_id`, and `department_id` from `Appointment.doctor_id -> Doctor.specialty_id`), never re-supplied by the caller (same "derive, don't trust a duplicate client-supplied value" discipline every branch-scoped module this session has used).
   - **Walk-in check-in** (no appointment yet): caller supplies `branch_id` + `department_id` directly. The resulting token is schema-legal but has no patient linkage at all — this is a real, schema-imposed limitation (not an oversight of this PRP) worth stating plainly in the docstring rather than inventing a `patient_id` column that isn't in `database/schema.sql`.

2. **`token_number` is a per-day, per-(branch, department) sequential integer computed on the fly, not a stored running counter.** `queue_tokens` has no "queue date" column to partition a resettable sequence against, and adding one would be a schema change beyond this PRP's stated scope (this module adds no new tables/columns). Instead, `token_number = COUNT(*) WHERE branch_id/department_id match AND checked_in_at::date = today, + 1` at check-in time. This resets naturally at midnight (today's count starts back at 0) without any stored "last number" state, and needs no locking beyond the same DB transaction the INSERT already runs in — a small, acceptable race window (two simultaneous check-ins could rarely compute the same number) is fine for a display token number with no uniqueness constraint behind it, unlike every other number in this codebase that actually gates resource access (bed labels, invoice items, etc.). This is a deliberate, documented tradeoff, not a missed edge case.

3. **Status transitions are a small explicit state machine**, same discipline as `services/lab_service.py`'s `_TRANSITION_ROLES` / `services/billing_engine.py`'s `_LEGAL_CLAIM_TRANSITIONS`:
   ```
   waiting         -> in_consultation | delayed | skipped
   delayed         -> waiting | in_consultation | skipped
   skipped         -> waiting            (re-queued)
   in_consultation -> done
   done            -> (terminal, no further transitions)
   ```
   `called_at` is set the first time a token transitions into `in_consultation` (from `waiting` or `delayed`). `completed_at` is set on transition into `done`. An illegal transition raises a dedicated exception mapped to 409, same pattern as every prior state-machine module.

4. **Every check-in and every status transition broadcasts over the existing WebSocket channel** (`app/websocket/queue_board.py`'s `manager.broadcast(branch_id, department_id, message)`) — this channel and its `ConnectionManager` already exist and are reused as-is, not modified. Because `broadcast` is `async`, the service functions that call it must themselves be `async def` (mixing sync and async endpoints in the same FastAPI app is fine, and every DB call inside them stays a plain synchronous SQLAlchemy call on the request's own connection — no new async DB machinery is introduced). This mirrors the one deliberate architectural difference already established for this module in `docs/ARCHITECTURE.md` §7 ("pushed over a WebSocket topic scoped per (branch_id, department_id)") — every other module's service layer this session has been plain sync `def`; this is the first and only one that needs to differ, and only for this reason.

5. **A new consumer worker, `workers/queue_wait_time_consumer.py`, subscribes to the already-published `queue.wait_time_updated` topic** (published by `services/scheduling_engine.py`'s `recalculate_downstream_wait_times`, payload: `appointment_id`, `doctor_id` — confirmed by reading that function, no other fields exist in the payload). Mirrors `workers/notification_consumer.py`'s exact shape (own durable queue, bound to exactly this one topic — not a wildcard, per that module's own REVIEW-AGENT-driven fix earlier this session): on receipt, looks up the `QueueToken` for that `appointment_id` (if one exists and is still `waiting`/`delayed` — a token already `in_consultation`/`done`/`skipped` has nothing left to re-estimate), recomputes `estimated_wait_minutes`, and rebroadcasts over the WebSocket channel so a connected queue-board client sees the update live, exactly as §7 describes ("recalculation of downstream estimated wait times ... and rebroadcast").
   - **Honest gap, stated plainly**: `recalculate_downstream_wait_times` is never actually CALLED by anything today (confirmed by grep — it's referenced only in a docstring in the old `routers/queue.py` stub and in `notification_engine.py`'s own docstring). Wiring a call to it into `services/consultation_service.py`'s `complete_consultation` (the "consultation-duration overrun" trigger point §7 describes) would mean reopening an already-shipped, already-reviewed module from earlier this session — the exact "retrofit a settled module" scope creep the Notification Hub PRP explicitly declined for the same reason (see that PRP's module overview point 2). This PRP makes the same call: it builds the consumer correctly and completely, ready to react the moment something publishes `queue.wait_time_updated` for a real reason, but does NOT go back and add that trigger call into Consultation. A future, explicitly-scoped PRP is the right place for "wire consultation-overrun detection," not a side effect of this one.
   - **Estimated-wait heuristic**: since `recalculate_downstream_wait_times`'s payload carries no computed delay (just `appointment_id`/`doctor_id`), the consumer's own recompute is a simple, explicitly placeholder heuristic — `estimated_wait_minutes = (count of waiting/delayed tokens ahead of this one in the same branch+department, by `checked_in_at`) * a fixed `AVERAGE_CONSULTATION_MINUTES` constant` — same "deliberately simple and replaceable" spirit `scheduling_engine.score_no_show_risk`'s own docstring already uses for a different heuristic in this exact file. Not a real queueing-theory model; a documented swap-in point.

6. **RBAC**: `front_desk` and `nurse` (the two roles who actually run an arrivals desk / call patients into a room) plus `system_admin` may check in and transition status. `doctor` may only read the live queue snapshot (so a doctor can see who's waiting without being able to alter the queue) — added to the read-only GET's role list alongside `front_desk`/`nurse`/`system_admin`. This mirrors Ward's `front_desk` read-only-vs-nurse/doctor-write split, adapted to this module's actual staff workflow (a doctor doesn't operate the front desk, but should be able to glance at their own queue).
   - Tenant guard: `QueueToken.branch_id` is a real column — `authorize(current_user, "queue_token", "read"/"write", token)` uses it directly, same as every branch-scoped resource this session. `@policy(...)` registrations needed: `front_desk`/`nurse` read+write, `doctor` read only. `system_admin` covered by the existing wildcard.

## SERVICE DESIGN — `backend/app/services/queue_service.py`

```python
class IllegalTokenStatusTransition(Exception): ...
class QueueTokenNotFoundError(Exception): ...
class AppointmentNotFoundError(Exception): ...

_LEGAL_TRANSITIONS: dict[TokenStatus, set[TokenStatus]] = {
    TokenStatus.waiting: {TokenStatus.in_consultation, TokenStatus.delayed, TokenStatus.skipped},
    TokenStatus.delayed: {TokenStatus.waiting, TokenStatus.in_consultation, TokenStatus.skipped},
    TokenStatus.skipped: {TokenStatus.waiting},
    TokenStatus.in_consultation: {TokenStatus.done},
    TokenStatus.done: set(),
}

AVERAGE_CONSULTATION_MINUTES = 15  # placeholder heuristic constant, see design decision #5

async def check_in(db: Session, current_user: User, payload: QueueCheckInRequest) -> QueueToken: ...
async def update_status(db: Session, current_user: User, token_id: uuid.UUID, new_status: TokenStatus) -> QueueToken: ...
def list_queue(db: Session, current_user: User, branch_id: uuid.UUID, department_id: int | None) -> list[QueueToken]: ...  # plain sync read, no broadcast involved
```

`check_in` and `update_status` are `async def` (design decision #4); `list_queue` (the read-only snapshot endpoint) stays plain sync `def` like every other read in this codebase, since it never broadcasts.

## ENDPOINTS

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/api/v1/queue/check-in` | front_desk, nurse, system_admin | body: `{appointment_id}` OR `{branch_id, department_id}` — exactly one shape, validated in the schema |
| PATCH | `/api/v1/queue/{token_id}/status` | front_desk, nurse, system_admin | body: `{status: "in_consultation"\|"delayed"\|"skipped"\|"waiting"\|"done"}` |
| GET | `/api/v1/queue/board` | front_desk, nurse, doctor, system_admin | query: `branch_id` (required), `department_id` (optional) — live snapshot, same data the WebSocket pushes, for initial page load before any broadcast has fired |

`GET /ws/queue/{branch_id}/{department_id}` (the existing WebSocket route) is unchanged — reused as-is.

## FILES TO CREATE / MODIFY

- `backend/app/schemas/queue.py` (new) — `QueueCheckInRequest`, `StatusUpdateRequest`, `QueueTokenResponse`.
- `backend/app/services/queue_service.py` (new) — as above.
- `backend/app/workers/queue_wait_time_consumer.py` (new) — mirrors `workers/notification_consumer.py`'s shape exactly (own queue, bound only to `queue.wait_time_updated`).
- `backend/app/routers/queue.py` (replace stub) — three endpoints above.
- `backend/app/core/security.py` — add `@policy("front_desk"/"nurse", "queue_token", "read"/"write")` (4 registrations; `doctor` gets read only, `system_admin` needs no separate entry per the existing wildcard).
- `backend/app/main.py` — move `queue.router` from the "Stubs" section into "Implemented".
- `docker-compose.yml` — add a `queue_wait_time_worker` service, same shape as the existing `notification_worker` service.
- `frontend/src/types/index.ts` (append) — `TokenStatus`, `QueueToken` types.
- `frontend/src/services/queueService.ts` (new) — `checkIn`, `updateStatus`, `getBoard`, plus a small WebSocket-subscribing helper for the live channel (`ws://.../ws/queue/{branchId}/{departmentId}`).
- `frontend/src/pages/QueueBoardPage.tsx` (new) — live queue board: loads the initial snapshot via `GET /board`, then subscribes to the WebSocket for live updates; front_desk/nurse/system_admin get check-in + status-transition controls, doctor sees a read-only view (same "buttons gated by role inside one shared page" discipline as `BedMatrixPage.tsx`/`InvoicePage.tsx`).
- `frontend/src/App.tsx` (append) — one route, `/queue/board`, `allowedRoles={["front_desk", "nurse", "doctor", "system_admin"]}`.

## PHASE EXECUTION PLAN

- **Phase 1** (single BACKEND-AGENT): `services/queue_service.py` + the state machine. Validation Gate 1: live DB round-trip (check-in creates a real row, status transitions update it correctly, illegal transitions raise) against the dev Postgres — WebSocket broadcast can be smoke-tested with a throwaway script or simply verified not to raise, since a full WS client round-trip is more of a Phase 3 concern.
- **Phase 2** (BACKEND-AGENT + FRONTEND-AGENT in parallel): schemas/router/policy/worker/compose (backend) + types/service/page/route (frontend), identical API contract given to both.
- **Validation Gate 2**: `py_compile`/`import app.main`, `tsc --noEmit`, `docker compose config`.
- **Phase 3** (TEST-AGENT + REVIEW-AGENT in parallel): TEST-AGENT covers the state machine (every legal/illegal transition), token-number computation, appointment-linked vs. walk-in check-in, RBAC gating, and the consumer's recompute heuristic. REVIEW-AGENT checks: is `authorize()` called before any mutation; does the async/sync split actually work correctly end-to-end; is the consumer's queue binding scoped to exactly one topic (not a wildcard, per the Notification Hub's own established fix); does `token_number`'s per-day computation actually reset correctly at a day boundary; is the "no retrofit into consultation_service" scope boundary from design decision #5 actually honored (i.e., confirm this PRP's diff never touches `consultation_service.py`).

## VALIDATION GATES

1. Gate 1 (Phase 1): live DB check-in + transition round-trip.
2. Gate 2 (Phase 2): backend compiles/imports, frontend `tsc --noEmit` clean, `docker compose config` validates the new worker service.
3. Gate 3 (Phase 3): new tests pass, full suite shows no new failures, REVIEW-AGENT findings applied.
