"""Seeds a reproducible demo dataset: one hospital group, one branch, one
specialty, one consultation room, one doctor (with a shift covering the next
30 days so the "available slots" endpoints have something to return), one
login per staff role, one patient-portal login -- and, in phase 2, actual
functional data to look at: three patients, a completed + a booked
appointment (with a consultation on the completed one), a ward with beds and
one active admission, a pharmacy inventory item + batch, a queue token, and
an invoice with a real chargeable line item.

HMS Project Completion Prompt gap ("Sample Data" / "Demo accounts"): before
this script, the only seeded data in this repository was clinical reference
data (drugs/interactions, `database/seed_clinical_reference_data.sql`) --
zero patients, staff, appointments, wards, or bills existed anywhere, so
there was nothing to demo or screenshot without registering everything by
hand through the UI first. Every login used in `README.md`/
`docs/DEMO_SCRIPT.md` is created here, not documented as a one-off manual
step.

Safe to run more than once: phase 1 (accounts/branch/room/doctor) is guarded
per-row ("does this already exist" checks). Phase 2 (patients/appointments/
ward/pharmacy/queue/invoice) is guarded as a single block, keyed on whether
the first demo patient already exists -- simpler than per-row idempotency
for a seed script, at the cost of not "topping up" partial phase-2 state if
it was ever left half-created (which only happens if this script itself
crashes mid-phase-2; a normal re-run after a full success just skips it).

Usage:
    docker compose exec backend python scripts/seed_demo_data.py
    # or, without Docker, from backend/ with DATABASE_URL set:
    python scripts/seed_demo_data.py
"""

import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

sys.path.insert(0, ".")

from app.core.security import hash_password  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models.appointment import Appointment, AppointmentStatus  # noqa: E402
from app.models.billing import Invoice, InvoiceItem  # noqa: E402
from app.models.consultation import Consultation, Drug  # noqa: E402
from app.models.patient import Patient  # noqa: E402
from app.models.pharmacy import InventoryBatch, InventoryItem  # noqa: E402
from app.models.queue import QueueToken, TokenStatus  # noqa: E402
from app.models.resource import Doctor, DoctorShift, Room, Specialty  # noqa: E402
from app.models.tenant import Branch, HospitalGroup  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402
from app.models.ward import Admission, Bed, BedStatus, Ward  # noqa: E402
from app.config import settings  # noqa: E402

DEMO_GROUP_NAME = "Demo Hospital Group"
DEMO_BRANCH_NAME = "Demo General Hospital"
DEMO_SPECIALTY_NAME = "General Medicine"
DEMO_PASSWORD = "Demo123!"

# (email, full_name, role)
DEMO_STAFF = [
    ("admin@hms.demo", "Demo System Admin", UserRole.system_admin),
    ("doctor@hms.demo", "Demo Doctor", UserRole.doctor),
    ("nurse@hms.demo", "Demo Nurse", UserRole.nurse),
    ("frontdesk@hms.demo", "Demo Front Desk", UserRole.front_desk),
    ("labtech@hms.demo", "Demo Lab Tech", UserRole.lab_tech),
    ("pharmacist@hms.demo", "Demo Pharmacist", UserRole.pharmacist),
    ("billing@hms.demo", "Demo Billing Admin", UserRole.billing_admin),
]
DEMO_PATIENT_EMAIL = "patient@hms.demo"

# (full_name, dob, sex, phone) -- fictional data only.
DEMO_PATIENTS = [
    ("Aarav Sharma", date(1988, 4, 12), "M", "555-0101"),
    ("Priya Nair", date(1995, 9, 3), "F", "555-0102"),
    ("Rohan Mehta", date(1972, 12, 25), "M", "555-0103"),
]


def _mrn() -> str:
    import uuid

    return f"MRN{uuid.uuid4().hex[:10].upper()}"


def _seed_accounts(db) -> tuple[Branch, Specialty, Room]:
    group = db.execute(select(HospitalGroup).where(HospitalGroup.name == DEMO_GROUP_NAME)).scalar_one_or_none()
    if group is None:
        group = HospitalGroup(name=DEMO_GROUP_NAME)
        db.add(group)
        db.flush()
        print(f"Created hospital group: {group.id}")

    branch = db.execute(select(Branch).where(Branch.name == DEMO_BRANCH_NAME)).scalar_one_or_none()
    if branch is None:
        branch = Branch(hospital_group_id=group.id, name=DEMO_BRANCH_NAME, timezone="UTC")
        db.add(branch)
        db.flush()
        print(f"Created branch: {branch.id}")

    specialty = db.execute(select(Specialty).where(Specialty.name == DEMO_SPECIALTY_NAME)).scalar_one_or_none()
    if specialty is None:
        specialty = Specialty(name=DEMO_SPECIALTY_NAME)
        db.add(specialty)
        db.flush()
        print(f"Created specialty: {specialty.id}")

    room = db.execute(
        select(Room).where(Room.branch_id == branch.id, Room.name == "Demo Consultation Room")
    ).scalar_one_or_none()
    if room is None:
        room = Room(branch_id=branch.id, name="Demo Consultation Room", room_type="consultation")
        db.add(room)
        db.flush()
        print(f"Created room: {room.id}")

    db.commit()

    for email, full_name, role in DEMO_STAFF:
        existing = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if existing is not None:
            continue
        user = User(
            branch_id=branch.id if role != UserRole.system_admin else None,
            email=email,
            hashed_password=hash_password(DEMO_PASSWORD),
            full_name=full_name,
            role=role,
            is_active=True,
            is_verified=True,
        )
        db.add(user)
        db.flush()

        if role == UserRole.doctor:
            doctor = Doctor(user_id=user.id, branch_id=branch.id, specialty_id=specialty.id)
            db.add(doctor)
            db.flush()
            shift_start = datetime.now(timezone.utc)
            shift_end = shift_start + timedelta(days=30)
            db.add(
                DoctorShift(
                    doctor_id=doctor.id,
                    shift_range=f"[{shift_start.isoformat()},{shift_end.isoformat()})",
                )
            )
        db.commit()
        print(f"Created {role.value} login: {email}")

    if db.execute(select(User).where(User.email == DEMO_PATIENT_EMAIL)).scalar_one_or_none() is None:
        patient_user = User(
            branch_id=None,
            email=DEMO_PATIENT_EMAIL,
            hashed_password=hash_password(DEMO_PASSWORD),
            full_name="Demo Patient",
            role=UserRole.patient,
            is_active=True,
            is_verified=True,
        )
        db.add(patient_user)
        db.commit()
        print(f"Created patient login: {DEMO_PATIENT_EMAIL}")

    return branch, specialty, room


def _seed_functional_data(db, branch: Branch, room: Room) -> None:
    if db.execute(select(Patient).where(Patient.full_name == DEMO_PATIENTS[0][0])).scalar_one_or_none() is not None:
        print("Functional demo data already present, skipping phase 2.")
        return

    doctor = db.execute(
        select(Doctor).join(User, User.id == Doctor.user_id).where(User.email == "doctor@hms.demo")
    ).scalar_one()
    front_desk = db.execute(select(User).where(User.email == "frontdesk@hms.demo")).scalar_one()
    patient_login = db.execute(select(User).where(User.email == DEMO_PATIENT_EMAIL)).scalar_one_or_none()

    patients = []
    for i, (full_name, dob, sex, phone) in enumerate(DEMO_PATIENTS):
        patient = Patient(
            mrn=_mrn(),
            full_name=full_name,
            dob=dob,
            sex=sex,
            phone=phone,
            user_id=patient_login.id if i == 0 and patient_login is not None else None,
        )
        db.add(patient)
        patients.append(patient)
    db.flush()
    print(f"Created {len(patients)} patients")

    now = datetime.now(timezone.utc)
    past_start = now - timedelta(days=3)
    past_end = past_start + timedelta(minutes=20)
    completed_appt = Appointment(
        branch_id=branch.id,
        patient_id=patients[0].id,
        doctor_id=doctor.id,
        room_id=room.id,
        time_range=f"[{past_start.isoformat()},{past_end.isoformat()})",
        status=AppointmentStatus.completed,
    )
    future_start = now + timedelta(days=2)
    future_end = future_start + timedelta(minutes=20)
    booked_appt = Appointment(
        branch_id=branch.id,
        patient_id=patients[1].id,
        doctor_id=doctor.id,
        room_id=room.id,
        time_range=f"[{future_start.isoformat()},{future_end.isoformat()})",
        status=AppointmentStatus.booked,
    )
    db.add_all([completed_appt, booked_appt])
    db.flush()
    print("Created 1 completed + 1 booked appointment")

    consultation = Consultation(
        appointment_id=completed_appt.id,
        doctor_id=doctor.id,
        patient_id=patients[0].id,
        symptoms="Persistent cough and mild fever",
        started_at=past_start,
        ended_at=past_end,
    )
    db.add(consultation)
    db.flush()
    print("Created 1 consultation")

    ward = Ward(branch_id=branch.id, name="Demo General Ward", ward_type="general")
    db.add(ward)
    db.flush()
    beds = [Bed(ward_id=ward.id, label=f"Bed-{n}", status=BedStatus.available) for n in (1, 2, 3)]
    db.add_all(beds)
    db.flush()
    admission = Admission(
        patient_id=patients[2].id,
        bed_id=beds[0].id,
        stay_range=f"[{(now - timedelta(days=1)).isoformat()},)",
        admitted_by=front_desk.id,
    )
    beds[0].status = BedStatus.occupied
    db.add_all([admission, beds[0]])
    db.commit()
    print("Created 1 ward with 3 beds, 1 active admission")

    drug = db.execute(select(Drug).order_by(Drug.name).limit(1)).scalar_one_or_none()
    if drug is not None:
        inventory_item = InventoryItem(branch_id=branch.id, drug_id=drug.id, reorder_threshold=10)
        db.add(inventory_item)
        db.flush()
        db.add(
            InventoryBatch(
                inventory_item_id=inventory_item.id,
                batch_number="DEMO-BATCH-1",
                quantity=50,
                expiry_date=date.today() + timedelta(days=180),
            )
        )
        db.commit()
        print(f"Created pharmacy inventory for {drug.name} (50 units, batch DEMO-BATCH-1)")
    else:
        print("No drugs seeded yet -- skipping pharmacy inventory "
              "(run database/seed_clinical_reference_data.sql first for this step)")

    queue_token = QueueToken(
        branch_id=branch.id,
        appointment_id=booked_appt.id,
        department_id=doctor.specialty_id,
        token_number=1,
        status=TokenStatus.waiting,
    )
    db.add(queue_token)
    db.commit()
    print("Created 1 queue token (checked in for the booked appointment)")

    invoice = Invoice(patient_id=patients[0].id, branch_id=branch.id, status="open", total_amount=Decimal("500.00"))
    db.add(invoice)
    db.flush()
    db.add(
        InvoiceItem(
            invoice_id=invoice.id,
            source_type="consultation",
            source_id=consultation.id,
            description="Consultation fee",
            amount=Decimal("500.00"),
        )
    )
    db.commit()
    print("Created 1 open invoice with 1 line item")


_PLACEHOLDER_PHI_KEY = "change-me-32-byte-urlsafe-base64-fernet-key="


def main() -> None:
    if settings.phi_encryption_key == _PLACEHOLDER_PHI_KEY:
        # A real, hard-learned failure mode, not a hypothetical: seeding
        # Patient rows (phone/national_id/address/allergies are all
        # `EncryptedString` columns) under the class-default placeholder key
        # succeeds silently -- the data only becomes unreadable later, the
        # moment anything reads it back under the REAL key from `.env`
        # (`cryptography.fernet.InvalidToken`), by which point the original
        # plaintext is gone. Refuse up front instead of writing
        # undecryptable rows.
        raise SystemExit(
            "PHI_ENCRYPTION_KEY is still the placeholder default -- set a real key in "
            "backend/.env (see README.md's \"Environment variables\" section) before "
            "seeding patient data, or rows written now will be undecryptable later."
        )

    db = SessionLocal()
    try:
        branch, _specialty, room = _seed_accounts(db)
        _seed_functional_data(db, branch, room)

        print("\nAll demo accounts use the password:", DEMO_PASSWORD)
        print("Staff sign in at /login, the patient account at /patient/login.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
