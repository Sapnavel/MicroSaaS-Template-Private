# PRP: Pharmacy Inventory Module (FEFO)

> Implementation blueprint for parallel agent execution.
> Module PRP inside the existing Hospital Management & Appointment Booking
> System scaffold — builds on `docs/ARCHITECTURE.md`, `database/schema.sql`,
> the Auth module, and the `Drug` reference table the Clinical Consultation
> and Lab modules already established.

---

## METADATA

| Field | Value |
|-------|-------|
| **Product** | Hospital Management & Appointment Booking System |
| **Module** | Pharmacy Inventory (FEFO batch management) |
| **Version** | 1.0 |
| **Created** | 2026-07-28 |
| **Complexity** | Medium-High (the FEFO dispense engine is a real atomic-multi-row-decrement problem — same family of correctness concern as the scheduling engine's tri-resource lock and the Lab module's TOCTOU-guarded sample creation, at smaller scope) |

---

## MODULE OVERVIEW

**Description:** Tracks drug stock per branch as a set of expiry-dated
batches, dispenses against the earliest-expiring stock first (FEFO), and
surfaces low-stock and upcoming-expiry alerts.

**Why this module, why now:** It's the last of the modules that shares the
`Drug` reference table (Consultation, Lab, and now Pharmacy all reference
`drugs.id`), and it's the **first module in this build sequence where branch
tenancy actually applies**. `Patient` and `Consultation`/`LabOrder` were all
deliberately branch-agnostic (see their own PRPs' "Key design decision"
sections) — physical drug stock is the opposite: a batch of pills sitting in
Branch A's pharmacy is not interchangeable with the same drug sitting in
Branch B's. This means `core/security.py`'s generic ABAC tenant guard (built
in the Auth module, mostly unused since) finally has a natural resource to
guard here, without needing a manual ownership workaround like Consultation's
`doctor_id` fix or Lab's.

**What already exists (do not recreate):**
- `database/schema.sql` §9 — `inventory_items` (branch_id, drug_id,
  reorder_threshold, `UNIQUE(branch_id, drug_id)`), `inventory_batches`
  (inventory_item_id, batch_number, quantity `CHECK (quantity >= 0)`,
  expiry_date, received_at), `idx_batches_fefo` index on
  `(inventory_item_id, expiry_date)`.
- `backend/app/models/consultation.py` — `Drug` (the reference table
  `inventory_items.drug_id` points at; do not create a second drug table).
- `backend/app/core/security.py` — `authorize()`'s tenant guard (fires
  automatically for any resource with a `branch_id` attribute — no new
  registration needed for the *tenant* part; you may still need a
  `pharmacist`-role policy registration for the specific resource_type,
  since `authorize()` denies by default when no policy is registered even
  after the tenant guard passes).
- `backend/app/routers/pharmacy.py` — stub with a TODO list; this PRP
  replaces it.
- `backend/app/services/scheduling_engine.py` — mirror its rigor for the
  dispense engine: `SELECT ... FOR UPDATE` locking in a consistent order,
  all-or-nothing transactional semantics, explicit exception types.
- `backend/app/models/audit.py` — `record_audit_event`, called on every
  dispense and every batch receipt.

**Key design decisions this PRP makes (read before implementing):**
1. **Branch tenancy is real here.** Every `inventory_items`/
   `inventory_batches` operation is scoped to the caller's `branch_id`
   (from their JWT claims, same as every other module's tenant guard usage)
   — a pharmacist at Branch A must never see or dispense against Branch B's
   stock. `system_admin` bypasses, per the existing tenant-guard convention.
2. **FEFO ≠ "dispense whatever's oldest, including expired stock."** The
   schema's `idx_batches_fefo` supports ordering by `expiry_date ASC`, but
   the dispense engine must **exclude already-expired batches
   (`expiry_date < today`) from FEFO candidates entirely** — dispensing
   expired medication is a patient-safety violation the module's name
   ("First Expired, First Out") could be misread as endorsing if taken
   literally. An expired batch is not "first out," it's a write-off. This
   is a real design decision this PRP makes explicit, the same way the Lab
   module made explicit that the schema conflates two clinical steps into
   one transition.
3. **Dispense is all-or-nothing.** If the sum of available (non-expired,
   `quantity > 0`) batch quantities for a drug is less than the requested
   amount, the entire dispense request fails (409) with nothing decremented
   — a partial dispense (e.g. handing over half a course of antibiotics
   because stock ran out mid-transaction) is a worse outcome than a clean
   failure the pharmacist can act on (order more stock, substitute a drug).
4. **No dedicated "dispense record" table exists in schema.sql.** Traceability
   (which prescription, if any, this dispense was for) is captured in the
   audit log's metadata, not a new relational table — do not invent one;
   this is consistent with how the Lab module handled "no `processed_by`
   column" by working within the schema's actual shape rather than silently
   adding structure the DDL doesn't have.

**MVP Scope:**
- [ ] ORM models for `InventoryItem`, `InventoryBatch`
- [ ] Receive stock: create/update an `InventoryItem`, add an
  `InventoryBatch`
- [ ] **FEFO dispense engine**: atomic, all-or-nothing, excludes expired
  batches, locks batches in a consistent order
- [ ] Low-stock query (current total quantity below `reorder_threshold`)
- [ ] Expiry alert query (batches expiring within N days, not yet expired)
- [ ] Branch-scoped access via the existing `authorize()` tenant guard

---

## TECH STACK

| Layer | Technology |
|-------|------------|
| Backend | FastAPI + SQLAlchemy (no new infra) |
| Frontend | React + TypeScript |

---

## DATA MODEL (ORM models to add — schema.sql already has both tables)

- `InventoryItem`: `branch_id` (FK branches.id), `drug_id` (FK drugs.id),
  `reorder_threshold` (Integer, default 0). No `UUIDPrimaryKeyMixin`
  concerns beyond the standard `id` PK — but note the `UNIQUE(branch_id,
  drug_id)` constraint means "one stock record per drug per branch," so the
  service layer's "receive stock" flow must find-or-create this row, not
  always insert.
- `InventoryBatch`: `inventory_item_id` (FK inventory_items.id),
  `batch_number` (String), `quantity` (Integer — mirror the DB's
  `CHECK (quantity >= 0)` as a defense-in-depth `CheckConstraint` in the
  ORM, same pattern the Clinical Consultation module's REVIEW-AGENT added
  for `prescription_items.duration_days`), `expiry_date` (Date),
  `received_at` (DateTime, server_default=now).

---

## FEFO DISPENSE ENGINE DESIGN (read before implementing — this is the core logic)

`backend/app/services/pharmacy_engine.py`, mirroring the rigor of
`services/scheduling_engine.py` and the Lab module's concurrency fix:

```
dispense(db, branch_id, drug_id, quantity, actor, reference=None) -> DispenseResult
```

1. Resolve the `InventoryItem` for `(branch_id, drug_id)` — 404 if no such
   item exists at all (this branch has never stocked this drug).
2. Query candidate batches: `WHERE inventory_item_id = X AND quantity > 0
   AND expiry_date >= today() ORDER BY expiry_date ASC, id ASC` (the `id ASC`
   tiebreaker gives a deterministic lock order when two batches share an
   expiry date — same "consistent ordering prevents deadlock" reasoning
   `scheduling_engine.py` uses for its resource locks), with `.with_for_update()`
   — **lock every candidate batch up front**, not one at a time as you
   consume them, so two concurrent dispense calls against overlapping
   batches serialize cleanly instead of racing (the exact TOCTOU shape the
   Lab module's REVIEW-AGENT just found and fixed — do not repeat it here;
   build this one with the lock from the start).
3. Walk the locked batches in FEFO order, consuming from each until the
   requested quantity is satisfied. Track exactly how much was taken from
   each batch (`list[(batch_id, quantity_taken)]`) — needed for the audit
   log and the response.
4. If the sum of ALL locked batches' quantities is less than the requested
   amount: raise `InsufficientStockError` (caller turns this into 409) —
   do not decrement anything, do not commit. Compute this by summing the
   locked rows in Python before applying any decrement, not by decrementing
   as you go and rolling back on shortfall (simpler to reason about,
   avoids ever writing a value that then needs undoing).
5. On success: decrement each consumed batch's `quantity`, `record_audit_event`
   (action="pharmacy.dispensed", resource_type="inventory_item",
   metadata recording `drug_id`, `branch_id`, `quantity`, the per-batch
   breakdown, and `reference` if given — e.g. a prescription id, purely
   informational, not a DB relationship), commit.
6. Expired batches (`expiry_date < today()`) are invisible to this function
   entirely — they are never selected, never locked, never appear in the
   result. A separate concern (not this PRP's scope): a future "write off
   expired stock" endpoint would operate on them explicitly.

**Receive stock** (`receive_batch(db, branch_id, drug_id, batch_number,
quantity, expiry_date, reorder_threshold=None)`): find-or-create the
`InventoryItem` for `(branch_id, drug_id)` (update `reorder_threshold` if
provided and the item already existed — your call whether "provided" means
"non-None" or requires an explicit sentinel, document it), then insert the
`InventoryBatch`. This is a simpler, lower-risk write than dispense (no
concurrent-decrement race — an insert of a brand new batch doesn't contend
with anything), but still runs inside one transaction.

---

## ENDPOINTS

| Method | Endpoint | Auth | Description |
|--------|----------|------|--------------|
| POST | /api/v1/pharmacy/items | pharmacist, system_admin | `{drug_id, reorder_threshold}`. `branch_id` from `current_user.branch_id` (system_admin must supply it explicitly in the body, since they have no branch of their own — decide and validate this). Find-or-create semantics per above. |
| POST | /api/v1/pharmacy/batches | pharmacist, system_admin | `{drug_id, batch_number, quantity, expiry_date}` — same branch-resolution rule as above. 404 if no matching `InventoryItem`/drug exists (or auto-create the item with a default `reorder_threshold` — pick one, document it; auto-create with `reorder_threshold=0` is the more forgiving choice and matches "receiving stock for a drug this branch hasn't stocked before" being a normal first-time event). |
| POST | /api/v1/pharmacy/dispense | pharmacist, system_admin | `{drug_id, quantity, reference}` (reference optional, free text or an id string — not FK-validated, just stored in audit metadata). -> 200 with the per-batch breakdown. -> 409 `InsufficientStockError` (include how much WAS available, so the pharmacist knows the shortfall, not just "no"). -> 404 unknown item. |
| GET | /api/v1/pharmacy/low-stock | pharmacist, system_admin | Branch-scoped (from `current_user.branch_id`, or an explicit `branch_id` query param for system_admin). Items where `SUM(inventory_batches.quantity WHERE expiry_date >= today())` < `reorder_threshold`. |
| GET | /api/v1/pharmacy/expiring?within_days=N | pharmacist, system_admin | Batches with `today() <= expiry_date <= today() + N days` (default `N` — pick a sensible one, e.g. 30, document it), branch-scoped same as above. Does NOT include already-expired batches (see design decision #2) — this is an upcoming-expiry alert, not a "what's gone bad" report. |

Branch scoping: call `authorize(current_user, "inventory_item", "read"|"write",
inventory_item_or_a_resource_with_branch_id)` for every operation — the
tenant guard fires automatically since these resources have a real
`branch_id`. You will need to register `pharmacist`/`system_admin` policies
for `("inventory_item", "read")`/`("inventory_item", "write")` in
`core/security.py` (the tenant guard alone isn't enough — `authorize()`
denies by default if no policy is registered for the role at all, same
reminder every prior module's PRP has included).

---

## FILES TO CREATE / MODIFY

**Create:**
- `backend/app/models/pharmacy.py` — `InventoryItem`, `InventoryBatch`.
- `backend/app/services/pharmacy_engine.py` — the FEFO dispense engine above.
- `backend/app/services/pharmacy_service.py` — `receive_batch`, `dispense`
  (thin wrapper calling `pharmacy_engine`), `get_low_stock`, `get_expiring`,
  response shaping.
- `backend/app/schemas/pharmacy.py` — request/response schemas.
- `frontend/src/services/pharmacyService.ts`, `frontend/src/pages/PharmacyInventoryPage.tsx`
  (receive stock, view items/batches for the caller's branch),
  `frontend/src/pages/PharmacyDispensePage.tsx` (dispense form + low-stock/expiring alert panels).
- `backend/tests/test_pharmacy_engine.py`, `backend/tests/test_pharmacy.py`

**Modify:**
- `backend/app/models/__init__.py` — register new models.
- `backend/app/routers/pharmacy.py` — replace the stub.
- `backend/app/core/security.py` — register `inventory_item` read/write
  policies for `pharmacist` (system_admin already covered by the wildcard).
- `backend/app/main.py` — no change expected (router already included).

---

## PHASE EXECUTION PLAN

**Phase 1: Models + FEFO engine (sequential)**
- BACKEND-AGENT: `models/pharmacy.py`, `services/pharmacy_engine.py`.

**Validation Gate 1:** models import cleanly against a real Postgres,
`CheckConstraint` on `quantity >= 0` mirrors the DDL exactly; the FEFO
selection/allocation logic (given a set of batch quantities-by-expiry and a
requested amount, which batches get how much) is unit-testable as a pure
function separate from the DB-locking wrapper — same split
`prescription_safety.py`/`patient_matching.py` use (pure allocation math vs.
the function that actually queries+locks Postgres).

**Phase 2: Endpoints + frontend (parallel)**
- BACKEND-AGENT: `schemas/pharmacy.py`, `services/pharmacy_service.py`,
  `routers/pharmacy.py`, ABAC policy registration.
- FRONTEND-AGENT: inventory + dispense pages.

**Validation Gate 2:** `ruff check backend/`, `mypy backend/app`, `npm run lint`, `tsc --noEmit`.

**Phase 3: Quality (parallel)**
- TEST-AGENT: FEFO allocation across multiple batches (splits correctly,
  earliest expiry consumed first), all-or-nothing on insufficient stock
  (nothing decremented, exact shortfall reported), expired batches never
  selected even if they'd otherwise be "earliest," concurrent dispense
  calls against the same item serialize correctly (two dispenses that
  together exceed available stock — one succeeds, one gets a clean 409, no
  overselling), branch tenant isolation (a pharmacist at Branch A cannot
  dispense/view Branch B's stock), low-stock/expiring queries.
- REVIEW-AGENT: same bar as every prior module. Specifically: is the
  locking order in `dispense` actually consistent enough to prevent
  deadlock if two dispense calls target overlapping sets of batches in
  different orders? Is there any write path to `InventoryBatch.quantity`
  that bypasses the lock (e.g. does `receive_batch` ever need to touch an
  existing batch's quantity, and if so, is that also guarded)? Is the
  expired-batch exclusion actually applied everywhere it needs to be
  (dispense candidate selection AND the expiring-soon query shouldn't
  double-count something already expired)? PHI is not a concern in this
  module (drug/quantity/expiry data isn't PHI) but branch-isolation
  correctness is the equivalent-severity concern here — treat a
  cross-branch stock leak with the same seriousness prior reviews gave PHI
  leaks.

---

## VALIDATION GATES

| Gate | Commands |
|------|----------|
| 1 | Apply schema.sql to fresh Postgres; import new models; unit-test the pure FEFO allocation function with no DB |
| 2 | `ruff check backend/`, `mypy backend/app`, `npm run lint`, `tsc --noEmit` |
| 3 | `pytest backend/tests/test_pharmacy_engine.py backend/tests/test_pharmacy.py --cov=app.services.pharmacy_engine --cov=app.services.pharmacy_service --cov=app.routers.pharmacy --cov-fail-under=80` |

---

## NEXT STEP

```
/execute-prp PRPs/pharmacy-module-prp.md
```
