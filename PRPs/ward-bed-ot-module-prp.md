# PRP: Ward, Bed & Operation Theatre (OT) Management Module

> Implementation blueprint for parallel agent execution.
> Module PRP inside the existing Hospital Management & Appointment Booking
> System scaffold — builds on `docs/ARCHITECTURE.md`, `database/schema.sql`,
> and **directly reuses** `backend/app/services/scheduling_engine.py`'s
> locking primitives rather than reinventing them.

---

## METADATA

| Field | Value |
|-------|-------|
| **Product** | Hospital Management & Appointment Booking System |
| **Module** | Ward, Bed & OT Management |
| **Version** | 1.0 |
| **Created** | 2026-07-28 |
| **Complexity** | Medium-High (same concurrency family as the scheduling engine — a physical resource booked over a time range — but with a real, honest gap: OT surgeon-conflict prevention has no DB backstop, only an app-level check, and this PRP says so plainly rather than pretending otherwise) |

---

## MODULE OVERVIEW

**Description:** Tracks physical bed occupancy across wards (admit,
discharge, transfer) and Operation Theatre scheduling (room + surgeon +
time), both modeled the same way appointments already are in this
system: a resource id plus a `TSTZRANGE`, protected by a Postgres
`EXCLUDE USING gist` constraint.

**Why this module, why now:** It's flagged in `docs/ARCHITECTURE.md` §9 as
independent of the clinical modules (Consultation/Lab/Pharmacy) — it can be
built without any of them, and doesn't depend on anything built so far
except `Patient`, `Doctor`, and `Room` (all already in place). It's also a
direct structural cousin of `services/scheduling_engine.py`: same
concurrency shape (lock, then insert-or-409-on-constraint-violation), same
resource-locking discipline — this PRP's job is to **apply that existing
pattern**, not invent a new one.

**What already exists (do not recreate):**
- `database/schema.sql` §10 — `wards` (branch_id, name, ward_type),
  `beds` (ward_id, label, `bed_status` enum: available/occupied/cleaning/blocked,
  `UNIQUE(ward_id, label)`), `admissions` (patient_id, bed_id, `stay_range
  TSTZRANGE`, admitted_by, discharged_at, `EXCLUDE USING gist (bed_id WITH
  =, stay_range WITH &&)`), `ot_schedules` (room_id, patient_id,
  surgeon_id, `time_range TSTZRANGE`, `EXCLUDE USING gist (room_id WITH =,
  time_range WITH &&)`).
- `backend/app/core/locking.py` — `lock_manager` (the Redis distributed
  lock, `acquire_all(resource_keys: list[str])`, already handles sorted
  lock ordering to prevent deadlock). **Reuse this directly** — do not
  build a second lock manager. This is the same object
  `scheduling_engine.py` uses for doctor/room/equipment locks; bed and OT
  locking uses the identical primitive with different key prefixes
  (`f"bed:{bed_id}"`, `f"room:{room_id}"`, `f"surgeon:{surgeon_id}"`).
- `backend/app/services/scheduling_engine.py` — the reference
  implementation for "lock resources, then let a `EXCLUDE` constraint be
  the authoritative backstop, catching `IntegrityError` and turning it into
  a clean 409." Read `book_appointment` closely; `admit_patient` and
  `schedule_ot` below follow its shape almost line-for-line.
- `backend/app/models/appointment.py` — the `TSTZRANGE` + `ExcludeConstraint`
  ORM pattern to copy for `Admission`/`OTSchedule`.
- `backend/app/models/resource.py` — `Room` (OT scheduling uses rooms with
  `room_type='ot'`), `Doctor` (the OT's `surgeon_id`).

**Key design decisions this PRP makes (read before implementing):**
1. **Admissions use an open-ended range at admit time.** `stay_range` is
   created as `[start_time,)` (unbounded upper) — you don't know the
   discharge time when a patient is admitted. This correctly makes the
   `EXCLUDE` constraint reject any *other* admission attempt for that bed
   from the same moment onward, which is exactly right: two patients cannot
   occupy one physical bed at overlapping times, and an "ongoing" stay
   overlaps everything after it starts. **Discharge doesn't delete or
   replace the row — it `UPDATE`s `stay_range` to close the upper bound**
   (`[start_time, discharge_time)`), which is what actually frees the bed
   for a new admission going forward. This is the same "closing a range"
   idea, applied to a table this codebase hasn't used it on yet.
2. **Bed status has a realistic 4-state lifecycle, not just "occupied or
   not."** `available → occupied` (admit) `→ cleaning` (discharge — the bed
   isn't immediately available for a new patient) `→ available` (an
   explicit "mark clean" action). `blocked` is an orthogonal manual
   override (maintenance, infection control) reachable from `available` or
   `cleaning`, not part of the normal patient-flow chain. `occupied` is
   NEVER set directly by a status-update endpoint — it only ever results
   from a successful admission.
3. **OT surgeon-conflict prevention is app-level only — say so plainly, do
   not imply otherwise.** `ot_schedules`' `EXCLUDE` constraint only covers
   `room_id` — there is no DB constraint preventing the SAME surgeon from
   being double-booked across two different rooms, or between an OT slot
   and a regular clinic `appointments` row, because that would require a
   single exclusion constraint spanning two different tables, which
   Postgres cannot express. This PRP requires an **app-level surgeon
   availability check** (query both `ot_schedules` and `appointments` for
   the surgeon's `doctor_id` before booking, under the same Redis lock as
   the room), but this is explicitly the DB-level-authoritative-guard
   *missing* for one dimension of this booking, the same honest-limitation
   spirit as the Lab module's conflated verify step or the Patient module's
   national_id normalization gap. A future schema change (e.g. a
   surgeon-busy-interval table with its own exclusion constraint) would be
   the real fix; this PRP does not attempt to fake one with triggers.

**MVP Scope:**
- [ ] ORM models for `Ward`, `Bed`, `Admission`, `OTSchedule`
- [ ] Live bed matrix query (branch/ward/status filterable)
- [ ] Admit / discharge / transfer, reusing `lock_manager` + the
  `EXCLUDE`-then-catch-`IntegrityError` pattern
- [ ] Bed status transitions (cleaning→available, →blocked, blocked→available)
  as a small, explicit state machine (mirroring the Lab module's discipline)
- [ ] OT scheduling: room + surgeon double-booking prevention, with the
  surgeon half explicitly documented as app-level-only

---

## TECH STACK

| Layer | Technology |
|-------|------------|
| Backend | FastAPI + SQLAlchemy + Redis (`core/locking.lock_manager`, reused) |
| Frontend | React + TypeScript |

---

## DATA MODEL (ORM models to add — schema.sql already has all four tables)

- `Ward`: `branch_id` (FK branches.id), `name` (String), `ward_type` (String — plain TEXT in schema.sql, not a Postgres enum, match that).
- `BedStatus(str, enum.Enum)`: mirrors the real Postgres `bed_status` enum (`available, occupied, cleaning, blocked`) — same "confirm it's a real `CREATE TYPE`, not plain TEXT" check the Lab module's PRP required for `lab_sample_status`.
- `Bed`: `ward_id` (FK wards.id), `label` (String), `status` (`Enum(BedStatus, name="bed_status")`, default `available`), `UniqueConstraint("ward_id", "label")` mirroring schema.sql's `UNIQUE(ward_id, label)`.
- `Admission`: `patient_id` (FK patients.id), `bed_id` (FK beds.id), `stay_range` (`TSTZRANGE`), `admitted_by` (FK users.id), `discharged_at` (DateTime, nullable), `ExcludeConstraint(("bed_id", "="), ("stay_range", "&&"), using="gist")` — copy `backend/app/models/appointment.py`'s `Appointment` class's `__table_args__` pattern exactly for the SQLAlchemy `ExcludeConstraint` syntax.
- `OTSchedule`: `room_id` (FK rooms.id), `patient_id` (FK patients.id), `surgeon_id` (FK doctors.id), `time_range` (`TSTZRANGE`), `ExcludeConstraint(("room_id", "="), ("time_range", "&&"), using="gist")` — same pattern, room-only (see design decision #3 — do NOT add a surgeon_id exclusion here, schema.sql doesn't have one and inventing one on just this table wouldn't cover the cross-table `appointments` conflict anyway).

---

## ENGINE DESIGN (read before implementing — reuses, doesn't reinvent)

`backend/app/services/ward_engine.py`, structured like `scheduling_engine.py`:

### `admit_patient(db, *, patient_id, bed_id, start_time, admitted_by) -> Admission`
1. `lock_manager.acquire_all([f"bed:{bed_id}"])`.
2. Fast-path check: `bed.status == BedStatus.available` — if not, 409
   immediately (clear error before even trying the DB insert; this is a UX
   nicety, the `EXCLUDE` constraint below is what's authoritative for the
   actual overlap).
3. Insert `Admission(stay_range="[start_time,)", ...)`. Catch
   `IntegrityError` from the `EXCLUDE` constraint → 409 (the bed's `status`
   column can drift from reality if something bypasses this function, so
   the DB constraint — not the status flag — is the real guard, exactly
   the two-layer reasoning `scheduling_engine.py`'s own docstring gives for
   why it locks AND relies on the constraint).
4. Set `bed.status = BedStatus.occupied`, commit, audit-log
   (`action="ward.admitted"`).

### `discharge_patient(db, *, admission_id, discharged_by, discharge_time=None) -> Admission`
1. Load the admission; 404 if missing; 409 if `discharged_at` already set
   (already discharged — same "don't allow a double-transition" discipline
   the Lab module's state machine uses).
2. `lock_manager.acquire_all([f"bed:{admission.bed_id}"])`.
3. `UPDATE`s `stay_range` to close the upper bound at `discharge_time`
   (default now) — this is what actually frees the bed for a future
   admission; sets `discharged_at`.
4. Sets `bed.status = BedStatus.cleaning` (NOT `available` — see design
   decision #2). Commit, audit-log (`action="ward.discharged"`).

### `transfer_patient(db, *, admission_id, new_bed_id, actor_user_id) -> Admission`
Discharge-then-admit, atomically, in ONE transaction and ONE lock scope:
1. Lock BOTH beds (old + new) in ONE `lock_manager.acquire_all([...])` call,
   sorted (the lock manager already sorts keys internally — just pass both
   keys, do not acquire them in two separate calls, which would defeat the
   whole point of consistent ordering).
2. Close the old admission's `stay_range` (as in `discharge_patient` step
   3), set old bed → `cleaning`.
3. Create a new `Admission` row for `new_bed_id` starting now (same insert +
   `IntegrityError`-catch as `admit_patient`), set new bed → `occupied`.
4. One commit for both halves — a transfer that fails to free the old bed
   AND grab the new one is worse than one that does neither; there is no
   valid intermediate state.

### `set_bed_status(db, *, bed_id, requested_status, actor_user_id) -> Bed`
A small explicit transition table (mirroring `lab_workflow.py`'s
`is_legal_transition`, at much smaller scope): legal manual transitions are
`cleaning → available`, `available → blocked`, `cleaning → blocked`,
`blocked → available`. **`occupied` is never a legal target of this
function** (only `admit_patient` sets it) and **`occupied` is never a legal
source either** (you don't "mark a bed available" out from under an active
admission — it must go through `discharge_patient` first). Illegal
transitions → 409.

### `schedule_ot(db, *, room_id, patient_id, surgeon_id, start_time, duration_minutes, actor_user_id) -> OTSchedule`
1. `lock_manager.acquire_all([f"room:{room_id}", f"surgeon:{surgeon_id}"])`
   (sorted automatically by the lock manager — pass both keys in one call).
2. **App-level surgeon availability check** (see design decision #3): query
   `ot_schedules` for `surgeon_id` with an overlapping `time_range`, AND
   query `appointments` for `doctor_id == surgeon_id` with an overlapping
   `time_range` (excluding cancelled/no_show/preempted, same status filter
   `scheduling_engine.py` uses) — if either hits, 409 with a message that
   distinguishes "surgeon busy in another OT" from "surgeon has a
   conflicting clinic appointment."
3. Insert `OTSchedule`; catch `IntegrityError` from the room-only `EXCLUDE`
   constraint → 409 (this catches room conflicts even if step 2 had a bug
   or a race the lock didn't cover — the room half of this booking DOES
   have the two-layer guarantee, the surgeon half does not, per design
   decision #3).
4. Commit, audit-log (`action="ward.ot_scheduled"`).

---

## ENDPOINTS

| Method | Endpoint | Auth | Description |
|--------|----------|------|--------------|
| GET | /api/v1/wards/beds | nurse, doctor, front_desk, system_admin | Query params `branch_id`/`ward_id`/`status`, branch-scoped like Pharmacy (wards have a real `branch_id`). |
| POST | /api/v1/wards/admissions | nurse, doctor, system_admin | `{patient_id, bed_id, start_time}`. |
| PATCH | /api/v1/wards/admissions/{id}/discharge | nurse, doctor, system_admin | `{discharge_time}` optional. |
| PATCH | /api/v1/wards/admissions/{id}/transfer | nurse, doctor, system_admin | `{new_bed_id}`. |
| PATCH | /api/v1/wards/beds/{id}/status | nurse, system_admin | `{status: "available"\|"blocked"}` — the legal-transition table above. |
| POST | /api/v1/wards/ot-schedule | doctor, system_admin | `{room_id, patient_id, surgeon_id, start_time, duration_minutes}`. |
| GET | /api/v1/wards/ot-schedule | doctor, nurse, system_admin | Query params `room_id`/`surgeon_id`/date range. |

Branch scoping: `Ward` has a real `branch_id` (same tenancy situation as
Pharmacy) — `Bed`/`Admission` inherit it transitively through their ward;
`authorize()`'s tenant guard needs a resource with `.branch_id`, so pass
the `Ward` (or a lightweight stand-in carrying its `branch_id`, same
`_BranchScoped` pattern `pharmacy_service.py` uses) where relevant. Register
`nurse`/`doctor`/`front_desk` policies for whatever `resource_type` you
choose (e.g. `"ward"`) — `authorize()` denies by default otherwise, same
reminder every prior module's PRP has included.

---

## FILES TO CREATE / MODIFY

**Create:**
- `backend/app/models/ward.py` — `Ward`, `BedStatus`, `Bed`, `Admission`, `OTSchedule`.
- `backend/app/services/ward_engine.py` — the engine above.
- `backend/app/services/ward_service.py` — response shaping, thin
  endpoint-facing wrappers (mirrors the `*_engine.py`/`*_service.py` split
  every prior module with a "hard part" uses).
- `backend/app/schemas/ward.py` — request/response schemas.
- `frontend/src/services/wardService.ts`, `frontend/src/pages/BedMatrixPage.tsx`
  (the live bed grid + admit/discharge/transfer actions),
  `frontend/src/pages/OTSchedulePage.tsx`.
- `backend/tests/test_ward_engine.py`, `backend/tests/test_ward.py`

**Modify:**
- `backend/app/models/__init__.py` — register new models.
- `backend/app/routers/wards.py` — replace the stub.
- `backend/app/core/security.py` — register `ward` (or your chosen
  resource_type name) policies for nurse/doctor/front_desk.
- `backend/app/main.py` — no change expected (router already included).

---

## PHASE EXECUTION PLAN

**Phase 1: Models + engine (sequential)**
- BACKEND-AGENT: `models/ward.py`, `services/ward_engine.py`.

**Validation Gate 1:** models import cleanly against a real Postgres; the
`ExcludeConstraint` DDL for both `Admission` and `OTSchedule` matches
schema.sql exactly (same `CreateTable(...).compile(dialect=postgresql.dialect())`
check used to verify `Appointment`'s exclusion constraint originally); a
live round-trip proves the open-ended-range admit + close-on-discharge
flow actually works against Postgres (insert `[t,)@`, confirm a second
overlapping admit attempt for the same bed hits the `EXCLUDE` constraint,
then close the range and confirm a new admission for that bed AFTER the
discharge time succeeds).

**Phase 2: Endpoints + frontend (parallel)**
- BACKEND-AGENT: `schemas/ward.py`, `services/ward_service.py`,
  `routers/wards.py`, ABAC policy registration.
- FRONTEND-AGENT: bed matrix + OT scheduling pages.

**Validation Gate 2:** `ruff check backend/`, `mypy backend/app`, `npm run lint`, `tsc --noEmit`.

**Phase 3: Quality (parallel)**
- TEST-AGENT: admit/discharge/transfer happy paths and their DB-level
  overlap rejections (attempt a second overlapping admission for an
  occupied bed → 409, both via the fast-path status check AND — construct
  a scenario that bypasses the fast-path if possible — via the `EXCLUDE`
  constraint itself); bed status legal/illegal transitions (occupied is
  never reachable/leavable via the status-update endpoint); OT room
  conflict (DB-backed) vs. surgeon conflict (app-level-only — write a test
  that specifically proves the surgeon check catches a conflict against a
  regular clinic `appointments` row, not just another OT slot); transfer's
  atomicity (both halves succeed or neither does — simulate a failure
  partway and confirm no orphaned state, e.g. old bed stuck `occupied`
  with no matching admission).
- REVIEW-AGENT: same bar as every prior module. Specifically: is the
  transfer lock actually acquired as ONE call with both bed keys (not two
  sequential single-key acquisitions, which would reintroduce a
  lock-ordering deadlock risk this codebase has been careful about
  everywhere else)? Does anything set `bed.status = occupied` outside
  `admit_patient`, or read/rely on `bed.status` as if it were authoritative
  instead of the `EXCLUDE` constraint? Is the OT surgeon-conflict check
  actually querying BOTH `ot_schedules` and `appointments`, not just one?
  Branch tenant isolation (same seriousness as Pharmacy's review).

---

## VALIDATION GATES

| Gate | Commands |
|------|----------|
| 1 | Apply schema.sql to fresh Postgres; import new models; confirm `ExcludeConstraint` DDL matches; live open-range-admit/close-on-discharge round-trip |
| 2 | `ruff check backend/`, `mypy backend/app`, `npm run lint`, `tsc --noEmit` |
| 3 | `pytest backend/tests/test_ward_engine.py backend/tests/test_ward.py --cov=app.services.ward_engine --cov=app.services.ward_service --cov=app.routers.wards --cov-fail-under=80` |

---

## NEXT STEP

```
/execute-prp PRPs/ward-bed-ot-module-prp.md
```
