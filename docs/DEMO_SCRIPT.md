# Demo Script (~5 minutes)

HMS Project Completion Prompt deliverable (section 6.8): no video recording
tool is available in this environment, so this is the walkthrough script a
presenter would follow to record one. It exercises the nine required flows
against the actual running system (`docker compose up -d`, `http://localhost:3000`).

Seed demo accounts first if the dev DB is empty:
`docker compose exec backend python scripts/seed_demo_data.py` (see
`README.md`'s "Demo accounts" section for the full login list).

Each step lists: the role to log in as, the exact clicks, and what to point
out on screen.

---

## 1. Patient login and doctor search (~30s)

- Go to `/patient/login`, sign in as a patient account.
- Navigate to **Find a doctor**.
- Filter by branch and specialty.
- **Say:** "Doctor search is scoped to branch and specialty — no fee
  schedule is shown because this system doesn't have one (a deliberate
  scope decision, not an oversight)."
- Open **My timeline**. **Say:** "This merges the patient's appointments,
  consultations, lab orders, prescriptions, admissions, and invoices into
  one chronological view — before this existed, each of those was only
  reachable as its own separate page."

## 2. Appointment booking (~40s)

- Pick a doctor, choose a date/time, submit.
- **Say:** "There's no slot-grid picker yet — you enter a candidate time and
  the backend either confirms it or returns a 409 naming exactly which
  resource conflicted (doctor, room, or equipment). The exclusion
  constraints backing this are enforced at the database level, not just in
  application code."
- Show the booking succeed; note the appointment's no-show risk score is
  computed server-side at booking time (visible on the admin dashboard
  later, step 9).

## 3. Patient check-in and token (~30s)

- Log out, log in as a `front_desk` user.
- Go to **Queue board**, check the patient in against their appointment.
- **Say:** "This assigns a digital token and pushes it to every connected
  queue board over a live WebSocket — no polling."
- Point out the live position/estimated-wait display.
- Check the **Emergency / priority** box on a second check-in and show it
  jump to the top of the board. **Say:** "Priority tokens sort ahead of
  everyone else, but still fairly among themselves by arrival time — this
  is inherited automatically from an appointment's emergency flag, or set
  explicitly for a walk-in whose condition changed after arrival."

## 4. Doctor consultation (~40s)

- Log in as the `doctor` from step 2.
- Open the checked-in patient's consultation, record symptoms/diagnosis/notes.
- **Say:** "Clinical notes are encrypted at rest, not just access-controlled."

## 5. Prescription (~40s)

- From the same consultation, open **New prescription**.
- Add a drug that triggers an allergy or interaction warning (see the
  patient's allergy list or `database/seed_clinical_reference_data.sql`
  for a pair that collides).
- **Say:** "This is a real database check against the patient's recorded
  allergies and a drug-interaction table — not a static UI warning. The
  interaction dataset is a small, explicitly-labeled illustrative sample
  (7 pairs), not a clinical-grade formulary."
- Show the BLOCK vs. OVERRIDE-REQUIRED distinction if time allows.

## 6. Laboratory workflow (~40s)

- As the doctor, order a lab test from the consultation.
- Log in as `lab_tech`, open **Lab worklist**, walk the order through
  collected → processing → verified.
- **Say:** "Verifying a result publishes an event that queues a
  notification back to the ordering doctor — that's a real message queue
  (RabbitMQ), not a direct function call."

## 7. Pharmacy workflow (~40s)

- Log in as `pharmacist`, open **Pharmacy dispense**.
- Search for the prescribed drug, dispense a quantity.
- **Say:** "Dispensing draws from the earliest-expiring batch first (FEFO)
  and decrements stock atomically under a row lock — two dispense requests
  racing for the last few units can't both succeed."
- Open **Pharmacy inventory** to show the low-stock/expiring-batch alerts.

## 8. Billing and payment (~50s)

- Log in as `billing_admin`, open **Invoices**.
- Create an invoice for the patient, add the consultation/lab/prescription
  as chargeable line items.
- Apply a tax rate and/or discount, show the computed grand total.
- Click **Mark paid**, then **Download receipt** to show the generated PDF.
- **Say clearly:** "This is a simulated payment flow — marking an invoice
  paid is a staff-confirmed status change, not a real payment gateway
  charge. No Stripe/Razorpay integration exists in this system."

## 9. Admin dashboard (~30s)

- Log in as `system_admin`, open **Executive dashboard**.
- Show bed occupancy, revenue, no-show rate, and stock alerts, filtered by
  branch.
- **Say:** "Every number here is a live SQL aggregate against the same
  tables just used in this walkthrough — nothing on this page is mocked."

---

## Known limitations to mention if asked

- SMS/email notifications are a documented logging stub (no real
  Twilio/SMTP account wired up) — see `backend/app/core/notification_providers.py`.
- Rescheduling and available-slot-lookup APIs both exist and are tested
  (`PATCH /appointments/{id}/reschedule`,
  `GET /appointments/available-slots` / `GET /me/doctors/{id}/available-slots`),
  but neither has a frontend page wired up to it yet — booking today is
  still "enter a candidate time, get a clean 409 if it conflicts."
- No patient-facing live queue view (queue board is staff-only today).
