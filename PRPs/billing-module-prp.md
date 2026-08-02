# PRP: Billing, Ledger & Insurance Claims Engine

> Implementation blueprint for parallel agent execution.
> Module PRP inside the existing Hospital Management & Appointment Booking
> System scaffold — builds on `docs/ARCHITECTURE.md`, `database/schema.sql`,
> and consumes chargeable events from Consultation, Lab, Pharmacy (noted,
> not billed — see design decision #4), and Ward, all of which are already
> built.

---

## METADATA

| Field | Value |
|-------|-------|
| **Product** | Hospital Management & Appointment Booking System |
| **Module** | Billing, Ledger & Insurance Claims |
| **Version** | 1.0 |
| **Created** | 2026-07-31 |
| **Complexity** | Medium (no distributed-lock concurrency family this time — the hazard here is a classic single-row read-modify-write ledger balance, correctly solved with Postgres `SELECT ... FOR UPDATE` alone; the real complexity is state-machine discipline across two linked entities, `Invoice` and `InsuranceClaim`, and DB-level double-billing prevention) |

---

## MODULE OVERVIEW

**Description:** Aggregates chargeable clinical events (completed
consultations, verified lab orders, finalized prescriptions, discharged
admissions) into per-patient invoices, splits an invoice between an insurer
and the patient's copay, and tracks the insurance claim's adjudication
state through to payment.

**Why this module, why now:** `docs/ARCHITECTURE.md` §9 lists Billing as
depending on Consultation/Lab/Pharmacy/Ward "for chargeable events" — all
four are now built (this is the 6th module completed; Ward just finished).
Nothing else blocks it.

**What already exists (do not recreate):**
- `database/schema.sql` §11 — `invoices` (patient_id, branch_id, status,
  total_amount), `invoice_items` (invoice_id, source_type, source_id,
  description, amount — polymorphic link back to `consultations`/
  `lab_orders`/`prescriptions`/`admissions`), `insurance_claims`
  (invoice_id, payer_name, claim_amount, patient_copay, state). This PRP's
  Phase 1 adds CHECK constraints and a `UNIQUE(source_type, source_id)`
  constraint to these tables (see DATA MODEL below) — apply that DDL change
  before writing ORM models, the same "confirm the DB shape first" order
  every prior module has followed.
- `'billing_admin'` role already exists in the `user_role` enum
  (schema.sql §1) — this module's primary actor. No new role needed.
- `backend/app/models/consultation.py` — `Consultation.ended_at` (nullable;
  non-null = chargeable), `Prescription.status` (chargeable when
  `'finalized'` — see design decision #4 on why `'dispensed'` never
  actually happens in this codebase yet).
- `backend/app/models/lab.py` — `LabOrder.status` (`LabOrderStatus.attached`
  = chargeable, the terminal state of that module's workflow).
- `backend/app/models/ward.py` — `Admission.discharged_at` (non-null =
  chargeable).
- `backend/app/core/security.py` — `authorize()`, `policy()` decorator,
  `get_caller_branch_id()`. Register `billing_admin`/`front_desk` policies
  for a `"billing"` resource_type (see ENDPOINTS below) — `authorize()`
  denies by default otherwise, same reminder every prior module's PRP has
  included.
- `backend/app/models/audit.py` — `record_audit_event(...)`.

**Key design decisions this PRP makes (read before implementing):**

1. **No Redis lock manager here — deliberately.** Every prior module
   (`scheduling_engine`, `ward_engine`, `pharmacy_engine`) uses
   `core/locking.lock_manager` because it coordinates a *time-range or
   quantity resource* across independent Redis-backed processes before a
   DB constraint can arbitrate. Billing's concurrency hazard is different:
   it's a plain read-modify-write ledger balance on ONE row
   (`invoices.total_amount`). Postgres's own `SELECT ... FOR UPDATE`
   inside a single transaction is both necessary and sufficient for that —
   introducing a Redis lock on top would be pure ceremony. Do not add one.
   (`invoice_items_source_unique` is the actual authoritative guard against
   the one real race that matters — two concurrent attempts to bill the
   *same* clinical event twice — and a DB `UNIQUE` constraint handles that
   correctly without any lock at all.)

2. **`total_amount` is always recomputed from `invoice_items`, never
   incremented.** `add_invoice_item` does `SELECT invoice FOR UPDATE`, THEN
   inserts the item, THEN sets `invoice.total_amount = SELECT SUM(amount)
   FROM invoice_items WHERE invoice_id = :id` in the same transaction —
   not `invoice.total_amount += item.amount`. This avoids the exact class
   of "derived field silently drifts from its source of truth" bug this
   codebase has hit before (Pharmacy/Ward's `Bed.status` vs. the real
   admission row) by never trusting the field as anything but a cache
   recomputed fresh every write.

3. **Every mutating function does ONE locked read, not an unlocked
   pre-read followed by a locked re-read.** Ward's Phase 3 review found a
   real bug (`ward_engine.py`'s original H1 "fix") where an unlocked
   `db.get()` followed by a same-session `SELECT ... FOR UPDATE` returned
   the SAME stale, identity-mapped Python object instead of a refreshed
   row. Billing's functions don't need Ward's "unlocked pre-read just to
   learn a lock key" step at all (there's no Redis key to derive — the
   `invoice_id`/`claim_id` path parameter IS the lock target) — so don't
   add one. Structure every mutation as: begin transaction → `SELECT ...
   FOR UPDATE` by primary key → check state → mutate → commit. If for any
   reason a function ever needs to read the row before the locked section
   (it shouldn't, here), it MUST use
   `.execution_options(populate_existing=True)` on the locked re-fetch,
   exactly like `ward_engine.py`'s current (fixed) `discharge_patient`.

4. **`Prescription.status` never reaches `'dispensed'` in this codebase
   today — bill on `'finalized'`, and say so.** `Consultation`'s own
   module docstring notes dispensing was left out of scope for Pharmacy's
   Phase 1/2; nothing anywhere transitions a `Prescription.status` past
   `'finalized'`. This PRP does not fix that (out of scope) — it treats
   `status == 'finalized'` as the chargeable signal for prescriptions,
   which is honestly just "was this prescription ever written," not "was
   it actually filled." Document this plainly in
   `list_chargeable_events`'s docstring rather than silently treating
   `'finalized'` as if it meant "dispensed."

5. **Claim state and invoice state are NOT automatically linked.** There is
   no `payments` ledger table in this schema — nothing records whether a
   patient's copay was actually collected at the desk. So
   `set_claim_state` reaching `'paid'` does **not** automatically flip
   `invoice.status` to `'paid'`: that would silently assert "fully
   settled" when only the insurer's portion is confirmed. A separate,
   explicit `mark_invoice_paid` action (billing_admin only) is the sole way
   an invoice becomes `'paid'` — representing a human confirming all money
   owed (insurer payment + patient copay) has actually been collected,
   outside this system's tracking. This is the same "state the honest
   limitation plainly instead of faking a derivation" discipline as Ward's
   OT surgeon-conflict gap (design decision #3 there).

6. **`invoice_items` insertion validates the source event is real and
   belongs to the same patient as the invoice — not just any UUID.**
   `add_invoice_item` looks up the referenced row (by `source_type`) and
   checks (a) it exists, (b) its `patient_id` matches `invoice.patient_id`,
   (c) it is actually chargeable per design decision #4's per-type
   definitions. A source_id that fails any of these is a 400/404, not a
   silently-accepted phantom charge.

**MVP Scope:**
- [ ] Schema: CHECK constraints on `invoices.status`/`insurance_claims.state`,
  `invoice_items.amount > 0`, and the `UNIQUE(source_type, source_id)`
  double-billing guard; RLS for all three billing tables.
- [ ] ORM models for `Invoice`, `InvoiceItem`, `InsuranceClaim`.
- [ ] Chargeable-event discovery (what can still be billed for a patient at
  a branch, not yet in any `invoice_items` row).
- [ ] Invoice lifecycle: create (open) → add items → split (insurer +
  copay) → mark-paid, with void reachable from open/split.
- [ ] Insurance claim state machine: submitted → adjudicating →
  approved/denied, approved → paid.

---

## TECH STACK

| Layer | Technology |
|-------|------------|
| Backend | FastAPI + SQLAlchemy (row-level `FOR UPDATE` locking only — no Redis) |
| Frontend | React + TypeScript |

---

## DATA MODEL

### Schema changes (apply first — see `database/schema.sql` §11, already applied by me in this session)
- `invoices`: `CHECK (status IN ('open','split','paid','void'))`,
  `CHECK (total_amount >= 0)`.
- `invoice_items`: `CHECK (amount > 0)`,
  `CHECK (source_type IN ('consultation','lab_order','prescription','admission'))`,
  `UNIQUE (source_type, source_id)` — the double-billing backstop, see
  design decision #1.
- `insurance_claims`: `CHECK (state IN ('submitted','adjudicating','approved','denied','paid'))`,
  `CHECK (claim_amount >= 0)`, `CHECK (patient_copay >= 0)`.
- §13 RLS: `invoices` (real `branch_id`), `invoice_items`/`insurance_claims`
  (join through `invoice_id -> invoices.branch_id`, same "no branch_id
  column of its own" pattern as `beds`/`admissions`).

### ORM models (`backend/app/models/billing.py`, new file)
- `InvoiceStatus(str, enum.Enum)`: `open, split, paid, void`. Plain `String`
  column (NOT a SQLAlchemy `Enum`/Postgres `CREATE TYPE`) — matches
  schema.sql's plain `TEXT` + the new CHECK constraint, same convention as
  `Prescription.status` (confirm this against schema.sql before assuming —
  `bed_status`/`lab_sample_status` ARE real Postgres enums, this one is
  not, by design, per the existing DDL).
- `ClaimState(str, enum.Enum)`: `submitted, adjudicating, approved, denied, paid`. Same plain-`String` convention.
- `Invoice`: `patient_id` (FK patients.id), `branch_id` (FK branches.id),
  `status` (String, default `"open"`), `total_amount` (Numeric(12,2),
  default `0`), `created_at`.
- `InvoiceItem`: `invoice_id` (FK invoices.id), `source_type` (String),
  `source_id` (UUID, no FK — polymorphic, matches schema.sql), `description`
  (String), `amount` (Numeric(12,2)). Mirror the new
  `UniqueConstraint("source_type", "source_id")` in `__table_args__` so
  SQLAlchemy's own metadata matches the DB exactly (same discipline as
  `Admission`'s `ExcludeConstraint` mirroring schema.sql).
- `InsuranceClaim`: `invoice_id` (FK invoices.id), `payer_name` (String),
  `claim_amount` (Numeric(12,2)), `patient_copay` (Numeric(12,2), default
  `0`), `state` (String, default `"submitted"`), `updated_at`.

---

## ENGINE DESIGN

`backend/app/services/billing_engine.py` — five mutating functions, each a
single `SELECT ... FOR UPDATE` + mutate + commit + audit-log inside one
transaction (see design decisions #1-3 for why there's no Redis lock and no
unlocked pre-read):

### `create_invoice(db, *, patient_id, branch_id, actor_user_id) -> Invoice`
Plain insert, `status="open"`, `total_amount=0`. Commit, audit-log
(`action="billing.invoice_created"`). No FOR UPDATE needed (nothing to
race against yet — the row doesn't exist until this returns).

### `add_invoice_item(db, *, invoice_id, source_type, source_id, description, amount, actor_user_id) -> Invoice`
1. `SELECT Invoice FOR UPDATE` by `invoice_id`; 404 if missing.
2. 409 `InvalidInvoiceStateError` if `invoice.status != "open"` (can't add
   charges to a split/paid/void invoice).
3. Look up the referenced row by `source_type` (`Consultation`, `LabOrder`,
   `Prescription`, or `Admission`); 404 `SourceEventNotFoundError` if
   missing; 400 `SourceEventPatientMismatchError` if its `patient_id`
   doesn't match `invoice.patient_id`; 409 `SourceEventNotChargeableError`
   if it fails the per-type chargeable check (design decision #4):
   `Consultation.ended_at is not None`, `LabOrder.status ==
   LabOrderStatus.attached`, `Prescription.status == "finalized"`,
   `Admission.discharged_at is not None`.
4. Insert `InvoiceItem`; catch `IntegrityError` from
   `invoice_items_source_unique` → 409 `DuplicateChargeError` (this exact
   event was already billed on some invoice, possibly a different one —
   the authoritative guard, not just this function's own in-memory check).
5. Recompute `invoice.total_amount = SELECT SUM(amount) FROM invoice_items
   WHERE invoice_id = :id` (design decision #2 — never `+=`).
6. Commit, audit-log (`action="billing.item_added"`, metadata includes
   `source_type`/`source_id`/`amount`).

### `split_invoice(db, *, invoice_id, payer_name, claim_amount, patient_copay, actor_user_id) -> Invoice`
1. `SELECT Invoice FOR UPDATE`; 404 if missing; 409 if `status != "open"`.
2. 422 `ClaimAmountMismatchError` if `claim_amount + patient_copay !=
   invoice.total_amount` (exact decimal equality — this is money, don't
   round-tolerate a mismatch).
3. Insert `InsuranceClaim(state="submitted")`.
4. Set `invoice.status = "split"`. Commit, audit-log
   (`action="billing.invoice_split"`).

### `set_claim_state(db, *, claim_id, requested_state, actor_user_id) -> InsuranceClaim`
Small explicit transition table, same shape as `ward_engine.py`'s
`_LEGAL_BED_TRANSITIONS`:
```python
_LEGAL_CLAIM_TRANSITIONS: set[tuple[ClaimState, ClaimState]] = {
    (ClaimState.submitted, ClaimState.adjudicating),
    (ClaimState.submitted, ClaimState.denied),
    (ClaimState.adjudicating, ClaimState.approved),
    (ClaimState.adjudicating, ClaimState.denied),
    (ClaimState.approved, ClaimState.paid),
}
```
`denied` and `paid` are terminal (never a source). `SELECT InsuranceClaim
FOR UPDATE`; 404 if missing; 409 `IllegalClaimStateTransition` (carries
`.current`/`.requested`) if `(current, requested)` not in the table. Does
**NOT** touch `invoice.status` — see design decision #5. Commit,
audit-log (`action="billing.claim_state_changed"`).

### `mark_invoice_paid(db, *, invoice_id, actor_user_id) -> Invoice`
`SELECT Invoice FOR UPDATE`; 404 if missing; 409 if `status not in
("open", "split")`. Set `status = "paid"`. Commit, audit-log
(`action="billing.invoice_paid"`).

### `void_invoice(db, *, invoice_id, actor_user_id) -> Invoice`
`SELECT Invoice FOR UPDATE`; 404 if missing; 409 if `status not in
("open", "split")` (a `paid` invoice needs a real refund process, out of
scope — voiding is for "this invoice should never have existed," not
undoing a completed payment). Set `status = "void"`. Commit, audit-log
(`action="billing.invoice_voided"`).

### `list_chargeable_events(db, *, patient_id, branch_id) -> list[ChargeableEvent]`
Pure read, lives in `billing_service.py` (not the engine — no mutation, no
lock needed, same reasoning Pharmacy's list endpoints use). For each of the
four source types, query rows matching `patient_id`, chargeable per design
decision #4, resolved to `branch_id` (`Consultation`/`LabOrder`/
`Prescription` via `consultation.appointment_id -> Appointment.branch_id`;
`Admission` via `bed_id -> Bed.ward_id -> Ward.branch_id`, reusing the exact
join Ward's own service already does) matching the requested `branch_id`,
and `NOT EXISTS` a matching `(source_type, source_id)` row in
`invoice_items`. Return a plain list of `(source_type, source_id,
suggested_description, event_date)` — no suggested amount (this system has
no fee schedule; `billing_admin` enters the amount manually on
`add_invoice_item`, see design decision #6's note that this is deliberate,
not a missing feature to silently fake with a made-up price list).

---

## ENDPOINTS

| Method | Endpoint | Auth | Description |
|--------|----------|------|--------------|
| POST | /api/v1/billing/invoices | billing_admin, system_admin | `{patient_id, branch_id}` → new open invoice. |
| GET | /api/v1/billing/invoices/{id} | billing_admin, front_desk, system_admin | Invoice + items + claim (if any). |
| GET | /api/v1/billing/patients/{patient_id}/invoices | billing_admin, front_desk, system_admin | `branch_id` query param required for non-system_admin. |
| GET | /api/v1/billing/patients/{patient_id}/chargeable-events | billing_admin, system_admin | `branch_id` query param required for non-system_admin. |
| POST | /api/v1/billing/invoices/{id}/items | billing_admin, system_admin | `{source_type, source_id, description, amount}`. |
| POST | /api/v1/billing/invoices/{id}/split | billing_admin, system_admin | `{payer_name, claim_amount, patient_copay}`. |
| PATCH | /api/v1/billing/claims/{id}/state | billing_admin, system_admin | `{state}` — legal-transition table above. |
| PATCH | /api/v1/billing/invoices/{id}/mark-paid | billing_admin, system_admin | No body. |
| PATCH | /api/v1/billing/invoices/{id}/void | billing_admin, system_admin | No body. |

`front_desk` is read-only (checkout needs to see balance due, never
mutates billing state) — no `doctor`/`nurse`/`lab_tech`/`pharmacist` access
at all (least privilege, matching Pharmacy's non-pharmacist role
restrictions). Register `billing_admin`/`front_desk` policies for
resource_type `"billing"` in `core/security.py` — `authorize()` denies by
default otherwise.

Branch scoping: `Invoice` has a real `branch_id` (set at creation — the
branch where the chargeable event happened, not floated across a patient's
whole multi-branch record, since `patients` intentionally has no
`branch_id` of its own — see `models/patient.py`). `InvoiceItem`/
`InsuranceClaim` inherit it transitively through `invoice_id`; pass the
loaded `Invoice` (or a `_BranchScoped` stand-in for the two branch-scoped
list/discovery GETs, same pattern `pharmacy_service.py` uses) to
`authorize()`.

---

## FILES TO CREATE / MODIFY

**Create:**
- `backend/app/models/billing.py` — `InvoiceStatus`, `ClaimState`,
  `Invoice`, `InvoiceItem`, `InsuranceClaim`.
- `backend/app/services/billing_engine.py` — the five mutating functions above.
- `backend/app/services/billing_service.py` — `list_chargeable_events`,
  authorization-wrapped thin endpoint-facing calls into the engine,
  response shaping (mirrors every prior `*_engine.py`/`*_service.py` split).
- `backend/app/schemas/billing.py` — request/response schemas.
- `frontend/src/services/billingService.ts`, `frontend/src/pages/InvoicePage.tsx`
  (create invoice, discover + add chargeable events, split, mark-paid,
  void), `frontend/src/pages/ClaimsPage.tsx` (claim state transitions).
- `backend/tests/test_billing_engine.py`, `backend/tests/test_billing_router.py`

**Modify:**
- `database/schema.sql` — §11 CHECK/UNIQUE constraints, §13 RLS (already
  applied in this session — Phase 1 should confirm, not redo, this).
- `backend/app/models/__init__.py` — register new models.
- `backend/app/routers/billing.py` — replace the stub.
- `backend/app/core/security.py` — register `billing` resource_type
  policies for `billing_admin`/`front_desk`.
- `backend/app/main.py` — no change expected (router already included).

---

## PHASE EXECUTION PLAN

**Phase 1: Models + engine (sequential)**
- BACKEND-AGENT: confirm the schema.sql changes (already applied) match
  this PRP exactly, then `models/billing.py`, `services/billing_engine.py`.

**Validation Gate 1:** models import cleanly against a real Postgres; the
`UniqueConstraint("source_type", "source_id")` DDL matches schema.sql
exactly; a live round-trip proves: (a) adding the same `(source_type,
source_id)` to two different invoices is rejected by the DB constraint,
not just an app-level check; (b) `add_invoice_item` recomputes
`total_amount` correctly after two sequential adds (not additive drift);
(c) `split_invoice` rejects a `claim_amount + patient_copay` that doesn't
exactly equal `total_amount`.

**Phase 2: Endpoints + frontend (parallel)**
- BACKEND-AGENT: `schemas/billing.py`, `services/billing_service.py`,
  `routers/billing.py`, ABAC policy registration.
- FRONTEND-AGENT: invoice + claims pages.

**Validation Gate 2:** `ruff check backend/`, `mypy backend/app`, `npm run lint`, `tsc --noEmit`.

**Phase 3: Quality (parallel)**
- TEST-AGENT: full invoice lifecycle (open → items → split → mark-paid);
  void from open and from split; claim state machine legal/illegal
  transitions (denied/paid terminal — no outgoing transition from either);
  `DuplicateChargeError` when the same source event is added twice, INCLUDING
  across two different invoices for the same patient (not just the same
  invoice twice); `SourceEventNotChargeableError` for an unfinished
  consultation/lab order/prescription/admission; `SourceEventPatientMismatchError`
  for a source event belonging to a different patient than the invoice;
  `ClaimAmountMismatchError` on a split whose numbers don't add up;
  concurrent `add_invoice_item` calls on the same invoice (confirm
  `total_amount` ends up correct, not lost-update-drifted — this is the
  `FOR UPDATE`-only concurrency claim, prove it under real concurrent
  sessions the way Pharmacy's FEFO test proved its own lock); router status
  codes (404/409/422) and RBAC (front_desk read-only, no other role has any
  access at all).
- REVIEW-AGENT: is `total_amount` ever incremented instead of recomputed
  from `invoice_items` (design decision #2)? Does any function do an
  unlocked pre-read before its `FOR UPDATE` re-fetch on the SAME row within
  the SAME session (the Ward H1 identity-map trap, design decision #3)? Is
  `invoice_items_source_unique`'s `IntegrityError` actually caught and
  turned into a clean 409, not left to bubble as a 500? Branch tenant
  isolation on all three tables (same seriousness as Ward/Pharmacy's own
  reviews). Does `set_claim_state` ever touch `invoice.status` (it must
  not, per design decision #5)?

---

## VALIDATION GATES

| Gate | Commands |
|------|----------|
| 1 | Apply schema.sql to fresh Postgres; import new models; confirm `UNIQUE(source_type, source_id)` DDL matches; live double-billing-rejected + total_amount-recompute + split-amount-mismatch round-trip |
| 2 | `ruff check backend/`, `mypy backend/app`, `npm run lint`, `tsc --noEmit` |
| 3 | `pytest backend/tests/test_billing_engine.py backend/tests/test_billing_router.py --cov=app.services.billing_engine --cov=app.services.billing_service --cov=app.routers.billing --cov-fail-under=80` |

---

## NEXT STEP

```
/execute-prp PRPs/billing-module-prp.md
```
