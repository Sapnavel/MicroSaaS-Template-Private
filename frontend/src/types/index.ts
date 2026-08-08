export interface Appointment {
  id: string;
  branchId: string;
  patientId: string;
  doctorId: string;
  roomId: string;
  status: "booked" | "checked_in" | "in_progress" | "completed" | "cancelled" | "no_show" | "preempted";
  isEmergency: boolean;
  triageLevel: number | null;
}

export type Role =
  | "patient"
  | "doctor"
  | "nurse"
  | "front_desk"
  | "lab_tech"
  | "pharmacist"
  | "billing_admin"
  | "system_admin";

export interface UserProfile {
  id: string;
  email: string;
  fullName: string;
  role: Role;
  branchId: string | null;
  hospitalGroupId: string | null;
  isActive: boolean;
  isVerified: boolean;
}

/** Camelcase mirror of the backend's TokenPair shape (access_token / refresh_token / token_type). */
export interface TokenPair {
  accessToken: string;
  refreshToken: string;
  tokenType: string;
}

/**
 * Fields always present on a patient record regardless of the caller's
 * role — see PRPs/patient-master-index-prp.md "ENDPOINTS" / ABAC note.
 * `dob` is the wire's `YYYY-MM-DD` string, not a Date, to avoid timezone
 * conversion surprises; format for display where needed.
 */
export interface PatientDemographics {
  id: string;
  mrn: string;
  fullName: string;
  dob: string;
  sex: string;
  phone: string;
  userId: string | null;
  mergedIntoId: string | null;
}

/**
 * Full record returned to doctor/nurse/system_admin callers. The clinical
 * fields are `string | null | undefined`, not just `string | null`:
 * `undefined` because the backend omits these JSON keys ENTIRELY for a
 * front_desk caller (role-shaped response, not merely empty values), and a
 * component rendering a `PatientFullRecord` it got back for a front_desk
 * user must not treat that absence as if it were a real value. `null`
 * covers a clinical caller whose patient simply has nothing on file for
 * that field.
 */
export interface PatientFullRecord extends PatientDemographics {
  nationalId?: string | null;
  address?: string | null;
  allergiesNote?: string | null;
}

/**
 * A pending (or resolved) row from the dedup review queue. `matchReason`
 * mirrors the backend's `patient_duplicate_candidates.match_reason` JSONB
 * column, which the PRP describes as recording "which signals fired and
 * their individual [numeric] contribution" to `matchScore` (e.g.
 * `{ phone_hash: 0.4, dob: 0.15 }`) -- the exact key set isn't specified by
 * the contract, so this is typed as a signal-name -> contribution map
 * rather than dumped as raw JSON in the UI.
 */
export interface DuplicateCandidate {
  id: string;
  patientA: PatientDemographics;
  patientB: PatientDemographics;
  matchScore: number;
  matchReason: Record<string, number>;
  status: "pending" | "confirmed_merge" | "rejected";
  createdAt: string;
}

/**
 * Clinical Consultation & Smart Prescription Engine types
 * (see PRPs/clinical-consultation-prescription-prp.md "ENDPOINTS").
 */

export interface Diagnosis {
  id: string;
  consultationId: string;
  icdCode: string;
  description: string;
  isPrimary: boolean;
}

/**
 * A record of a patient's known drug/substance allergy, used by the
 * prescription safety checker. Named `PatientAllergy` (not just `Allergy`)
 * to keep this distinct from the Patient module's free-text
 * `PatientFullRecord.allergiesNote` field, which is an unstructured intake
 * note, not a row in this structured, safety-checked table.
 */
export interface PatientAllergy {
  id: string;
  patientId: string;
  substance: string;
  severity: "mild" | "moderate" | "severe";
  reaction: string;
}

export interface PrescriptionItem {
  id: string;
  drugId: string;
  dosage: string;
  frequency: string;
  durationDays: number | null;
}

export interface Prescription {
  id: string;
  consultationId: string;
  patientId: string;
  status: "draft" | "finalized" | "dispensed";
  createdAt: string;
  items: PrescriptionItem[];
}

/**
 * Three-tier outcome of the prescription safety engine -- see the PRP's
 * "SAFETY ENGINE DESIGN" section. BLOCK is a hard stop (no override ever
 * possible), OVERRIDE_REQUIRED needs an explicit override + reason, INFO is
 * surfaced for awareness only and never blocks anything.
 */
export type SafetyTier = "BLOCK" | "OVERRIDE_REQUIRED" | "INFO";

export interface SafetyFinding {
  source: "allergy" | "interaction";
  severity: string;
  tier: SafetyTier;
  message: string;
  drugIds: string[];
}

export interface Consultation {
  id: string;
  appointmentId: string;
  doctorId: string;
  patientId: string;
  symptoms: string;
  notes: string | null;
  startedAt: string;
  endedAt: string | null;
  diagnoses: Diagnosis[];
  prescriptions: Prescription[];
}

/**
 * Result of a successful `POST /consultations/{id}/prescriptions` -- the
 * safety report is returned even on a clean pass so the prescriber sees
 * the INFO-tier findings that were checked, not just silent success.
 */
export interface PrescriptionCreateResult {
  prescription: Omit<Prescription, "items">;
  items: PrescriptionItem[];
  safetyReport: {
    allergyFindings: SafetyFinding[];
    interactionFindings: SafetyFinding[];
  };
}

/**
 * Lab module (sample tracking lifecycle) -- see PRPs/lab-module-prp.md
 * "ENDPOINTS" / "STATE MACHINE DESIGN". Forward-only:
 * ordered -> collected -> processing -> verified -> attached.
 */
export type LabOrderStatus = "ordered" | "collected" | "processing" | "verified" | "attached";

/**
 * Per-step evidence attached to a lab order once collection has happened.
 * `result` is `string | null | undefined`, not just `string | null`:
 * `undefined` because the backend omits the `result` JSON key entirely for
 * a nurse caller (nurses collect samples but the PRP's per-transition role
 * table never gives them a reason to see the PHI result value), the same
 * absence-vs-null distinction `PatientFullRecord`'s clinical fields use for
 * front_desk. `null` covers any other (authorized) caller whose order
 * simply hasn't reached the `verified` transition yet, where the result is
 * genuinely not produced yet rather than hidden.
 */
export interface LabSample {
  collectedBy: string | null;
  collectedAt: string | null;
  processedAt: string | null;
  verifiedBy: string | null;
  verifiedAt: string | null;
  result?: string | null;
  attachedToEmrAt: string | null;
}

export interface LabOrder {
  id: string;
  consultationId: string;
  patientId: string;
  testCode: string;
  orderedBy: string;
  status: LabOrderStatus;
  createdAt: string;
  sample: LabSample | null;
}

/**
 * Pharmacy Inventory module (FEFO batch management) -- see
 * PRPs/pharmacy-module-prp.md "ENDPOINTS" / "DATA MODEL". Branch tenancy is
 * real here (the PRP's "Key design decisions" #1): a batch of pills at
 * Branch A is not interchangeable with the same drug at Branch B. A
 * pharmacist's `branchId` is implicit from their own session
 * (`useAuth().user.branchId`); only a `system_admin` (who has no branch of
 * their own -- `branchId: null`) ever supplies one explicitly.
 */
export interface InventoryItem {
  id: string;
  branchId: string;
  drugId: string;
  reorderThreshold: number;
}

export interface InventoryBatch {
  id: string;
  inventoryItemId: string;
  batchNumber: string;
  quantity: number;
  expiryDate: string;
  receivedAt: string;
}

/** How much of a dispense request was drawn from one specific batch. */
export interface DispenseAllocation {
  batchId: string;
  batchNumber: string;
  expiryDate: string;
  quantityTaken: number;
}

/**
 * Result of a successful dispense -- the per-batch `allocations` breakdown
 * is the meaningful pharmacy information here (which batches were drawn
 * from, FEFO order), not just a quantity total.
 */
export interface DispenseResult {
  inventoryItemId: string;
  branchId: string;
  drugId: string;
  quantityDispensed: number;
  allocations: DispenseAllocation[];
}

export interface LowStockItem {
  inventoryItemId: string;
  branchId: string;
  drugId: string;
  drugName: string;
  currentQuantity: number;
  reorderThreshold: number;
}

export interface ExpiringBatch {
  batchId: string;
  inventoryItemId: string;
  drugId: string;
  drugName: string;
  batchNumber: string;
  quantity: number;
  expiryDate: string;
}

/**
 * Ward, Bed & OT Management module -- see
 * PRPs/ward-bed-ot-module-prp.md "ENDPOINTS" / "DATA MODEL". Mirrors the
 * real Postgres `bed_status` enum (available/occupied/cleaning/blocked).
 * `occupied` is only ever set by a successful admission -- never a legal
 * target (or source) of the manual status-update endpoint, per the PRP's
 * design decision #2.
 */
export type BedStatus = "available" | "occupied" | "cleaning" | "blocked";

/** One row of the live bed matrix -- GET /wards/beds. */
export interface BedMatrixEntry {
  id: string;
  wardId: string;
  wardName: string;
  branchId: string;
  label: string;
  status: BedStatus;
  /** The currently-open admission's id for this bed, or `null` when the bed
   * has no open admission (i.e. not occupied). Lets the UI look up the
   * admission id directly off the bed row for discharge/transfer, instead
   * of asking the user to type it in by hand. */
  activeAdmissionId: string | null;
}

/**
 * A patient's stay in one bed. `endTime`/`dischargedAt` are both `null` for
 * an ongoing stay -- the backend models this as an open-ended `stay_range`
 * (`[start_time,)`, per the PRP's design decision #1) -- and are both set
 * together once a discharge closes the range.
 */
export interface Admission {
  id: string;
  patientId: string;
  bedId: string;
  admittedBy: string;
  startTime: string;
  endTime: string | null;
  dischargedAt: string | null;
}

/**
 * One booked OT slot -- see PRPs/ward-bed-ot-module-prp.md "ENGINE DESIGN".
 * `endTime` is server-computed from `start_time + duration_minutes` at
 * booking time; there is no separate duration field on the read shape.
 */
export interface OTSchedule {
  id: string;
  roomId: string;
  patientId: string;
  surgeonId: string;
  startTime: string;
  endTime: string;
}

/**
 * Billing, Ledger & Insurance Claims module -- see
 * PRPs/billing-module-prp.md "ENDPOINTS" / "DATA MODEL". `InvoiceStatus`
 * mirrors the plain-`TEXT` + CHECK-constraint `invoices.status` column
 * (not a real Postgres enum, per the PRP's ORM-models note).
 */
export type InvoiceStatus = "open" | "split" | "paid" | "void";

/**
 * Mirrors `insurance_claims.state` -- `denied`/`paid` are terminal (never a
 * source in `_LEGAL_CLAIM_TRANSITIONS`), see the PRP's `set_claim_state`.
 */
export type ClaimState = "submitted" | "adjudicating" | "approved" | "denied" | "paid";

/**
 * Polymorphic link back to the clinical event an `InvoiceItem` charges for
 * -- `consultations`/`lab_orders`/`prescriptions`/`admissions`, see the
 * PRP's `invoice_items.source_type` CHECK constraint.
 */
export type SourceType = "consultation" | "lab_order" | "prescription" | "admission";

/** POST /billing/invoices, GET .../invoices/{id}, per-patient invoice list. */
export interface Invoice {
  id: string;
  patientId: string;
  branchId: string;
  status: InvoiceStatus;
  totalAmount: number;
  /** Percent, e.g. 8.5 -- null means no tax has been set. */
  taxRatePercent: number | null;
  discountAmount: number;
  /** Computed server-side: tax on (totalAmount - discountAmount). */
  taxAmount: number;
  /** Computed server-side: totalAmount - discountAmount + taxAmount. */
  grandTotal: number;
  createdAt: string;
}

/** One charge line on an invoice -- POST /billing/invoices/{id}/items. */
export interface InvoiceItem {
  id: string;
  invoiceId: string;
  sourceType: SourceType;
  sourceId: string;
  description: string;
  amount: number;
}

/** POST /billing/invoices/{id}/split, PATCH /billing/claims/{id}/state. */
export interface InsuranceClaim {
  id: string;
  invoiceId: string;
  payerName: string;
  claimAmount: number;
  patientCopay: number;
  state: ClaimState;
  updatedAt: string;
}

/**
 * GET /billing/patients/{patient_id}/chargeable-events -- a clinical event
 * that is chargeable and not yet on any invoice. No suggested amount: this
 * system has no fee schedule, so `billing_admin` types in the amount
 * manually when adding it to an invoice via `add_invoice_item` (deliberate,
 * see the PRP's design decision #6 -- not a missing feature).
 */
export interface ChargeableEvent {
  sourceType: SourceType;
  sourceId: string;
  suggestedDescription: string;
  eventDate: string;
}

/**
 * Notification & Alert Hub module -- see
 * PRPs/notification-hub-prp.md "ENDPOINTS" / "DATA MODEL". Mirrors the
 * plain-`TEXT` + CHECK-constraint `notifications.channel`/`status` columns
 * (not real Postgres enums, same convention as `InvoiceStatus`).
 */
export type NotificationChannel = "sms" | "email" | "push";

export type NotificationStatus = "queued" | "sent" | "failed";

/**
 * One row of notification history -- GET /notifications, PATCH
 * /notifications/{id}/retry. Named `NotificationRecord` (not `Notification`)
 * to avoid colliding with the browser's built-in `Notification` global.
 * `userId`/`patientId`/`branchId` are all nullable: not every notification
 * resolves all three (e.g. an unrecognized topic's row, or one keyed only
 * off a user rather than a patient) -- see the PRP's design decision #4.
 * `payload` is the arbitrary raw event payload that triggered this
 * notification -- its shape varies by which of the four event topics fired
 * it, so it's rendered as a generic JSON dump rather than given a bespoke
 * schema.
 */
export interface NotificationRecord {
  id: string;
  userId: string | null;
  patientId: string | null;
  branchId: string | null;
  channel: NotificationChannel;
  template: string;
  payload: Record<string, unknown>;
  status: NotificationStatus;
  createdAt: string;
}

/**
 * Executive & Operational Dashboard module -- see
 * PRPs/executive-dashboard-prp.md "MODULE OVERVIEW" / "SERVICE DESIGN".
 * This entire module is `system_admin`-only (design decision #1) -- no
 * other role ever renders any of these types.
 */

/** GET /dashboard/occupancy -- one row per ward. `occupancyPct` is a plain
 * fraction (0.0-1.0), not pre-multiplied by 100 -- multiply by 100 for
 * display, same as any other raw-ratio field in this codebase. */
export interface WardOccupancy {
  branchId: string;
  wardId: string;
  wardName: string;
  wardType: string;
  totalBeds: number;
  occupiedBeds: number;
  occupancyPct: number;
}

/** GET /dashboard/wait-times -- per (branch, department) average wait for
 * tokens that were actually called. Per design decision #2 this list is
 * legitimately empty in this deployment today: the Queue check-in module
 * (`routers/queue.py`) was never built, so `queue_tokens` has zero rows --
 * that is expected, not a bug. */
export interface DepartmentWaitTime {
  branchId: string;
  departmentId: number | null;
  departmentName: string | null;
  avgWaitMinutes: number;
  sampleSize: number;
}

/** GET /dashboard/revenue -- one row per (branch, day). `collected` is an
 * approximation bucketed by `Invoice.created_at`, not a true "cash received
 * on this date" figure -- see design decision #4 (this schema has no
 * `paid_at` column). */
export interface DailyRevenue {
  branchId: string;
  day: string;
  invoiceCount: number;
  grossBilled: number;
  collected: number;
  outstanding: number;
}

/** GET /dashboard/no-shows -- one row per branch. `noShowRate` is a plain
 * fraction (0.0-1.0) of ACTUAL outcomes. `avgPredictedNoShowRisk` is the
 * mean `scheduling_engine.score_no_show_risk` heuristic score assigned at
 * booking time for the same window's appointments -- `null` when every
 * considered row's score is still unset (e.g. rows booked before this
 * scoring existed). `noShowRiskScoreNote` explains the relationship between
 * the two; render it as a small caption, don't drop it. */
export interface NoShowRate {
  branchId: string;
  totalConsidered: number;
  noShowCount: number;
  noShowRate: number;
  avgPredictedNoShowRisk: number | null;
  noShowRiskScoreNote: string;
}

/** GET /dashboard/stock-alerts -- a thin bundle of the same
 * `LowStockItem`/`ExpiringBatch` shapes the Pharmacy module already
 * defines above (design decision #6: this dashboard endpoint calls
 * straight through to `pharmacy_service.get_low_stock`/`get_expiring`,
 * no reimplementation), so those existing types are reused as-is here. */
export interface StockAlerts {
  lowStock: LowStockItem[];
  expiringSoon: ExpiringBatch[];
}

/**
 * Real-Time Queue & Digital Token module -- see
 * PRPs/realtime-queue-prp.md "MODULE OVERVIEW" / "ENDPOINTS". Fills in the
 * exact gap the Executive Dashboard's `DepartmentWaitTime` comment above
 * already flagged (`routers/queue.py` was previously a stub).
 *
 * Per design decision #1, a `QueueToken` has no `patientId` of its own --
 * `appointmentId` is the only (nullable) link back to a patient, and a
 * walk-in check-in (no appointment) leaves it `null`.
 */
export type TokenStatus = "waiting" | "in_consultation" | "delayed" | "skipped" | "done";

export interface QueueToken {
  id: string;
  branchId: string;
  appointmentId: string | null;
  departmentId: number | null;
  tokenNumber: number;
  status: TokenStatus;
  checkedInAt: string;
  calledAt: string | null;
  completedAt: string | null;
  estimatedWaitMinutes: number | null;
  isPriority: boolean;
}

/**
 * Lookup/reference shapes backing the "raw UUID text box -> dropdown"
 * conversion across ~11 pages -- see the reusable `<XSelect>` components in
 * `components/selects/` and their matching `services/*.ts` files
 * (`branchService.ts`, `specialtyService.ts`, `doctorService.ts`,
 * `roomService.ts`, `wardService.ts#listWards`, `drugService.ts`,
 * `staffService.ts`, `appointmentService.ts`,
 * `billingService.ts#listClaims`, `consultationService.ts#listConsultations`).
 * These are select-list shapes only (just enough to render an option and
 * carry an id), not full domain records -- several already have a fuller
 * sibling type above (`Appointment`, `InsuranceClaim`, `Consultation`) for
 * their respective modules' own read/write flows.
 */
export interface Branch {
  id: string;
  name: string;
}

export interface Specialty {
  id: number;
  name: string;
}

export interface Doctor {
  id: string;
  fullName: string;
  specialtyId: number;
  branchId: string;
}

export interface Room {
  id: string;
  name: string;
  roomType: string;
  branchId: string;
}

export interface Ward {
  id: string;
  name: string;
  wardType: string;
  branchId: string;
}

export interface Drug {
  id: string;
  name: string;
  genericName: string | null;
}

export interface Staff {
  id: string;
  fullName: string;
  role: string;
  branchId: string | null;
}

/** `GET /api/v1/appointments` list-item shape for `<AppointmentSelect>` --
 * adds `patientName` (for a human-readable option label) and omits
 * `branchId` (the list is already branch-scoped by query param), so this is
 * deliberately a distinct, thinner shape from the fuller `Appointment` type
 * above rather than a reuse of it. */
export interface AppointmentListItem {
  id: string;
  patientId: string;
  patientName: string;
  doctorId: string;
  roomId: string;
  status: string;
  isEmergency: boolean;
  triageLevel: number | null;
}

/** `GET /api/v1/billing/claims` list-item shape for `<ClaimSelect>` -- adds
 * `patientName` (for a human-readable option label); distinct from the
 * fuller `InsuranceClaim` type above (which lacks `patientName` but has the
 * full claim/copay breakdown). */
export interface ClaimListItem {
  id: string;
  invoiceId: string;
  patientName: string;
  state: ClaimState;
  amount: number;
}

/** `GET /api/v1/consultations?patient_id=` list-item shape for
 * `<ConsultationSelect>` -- a thin summary (id + label material only),
 * distinct from the fuller `Consultation` type above. */
export interface ConsultationListItem {
  id: string;
  appointmentId: string;
  createdAt: string;
  doctorName: string;
}

/** One entry in a patient's unified timeline -- GET /api/v1/me/timeline
 * (patient) / GET /api/v1/patients/{id}/timeline (staff). `eventType` is a
 * plain string tag (`"appointment"`, `"consultation"`, `"lab_order"`,
 * `"prescription"`, `"admission"`, `"invoice"`), not a closed union --
 * unrecognized values render generically rather than being a type error,
 * matching the backend's own "additive tag, not a Literal" choice (see
 * services/patient_timeline_service.py's `TimelineEvent` docstring). */
export interface TimelineEvent {
  eventType: string;
  id: string;
  occurredAt: string;
  summary: string;
}
