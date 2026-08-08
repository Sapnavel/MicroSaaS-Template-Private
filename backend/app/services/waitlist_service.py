"""Appointment waiting list (HMS Project Completion Prompt, section 3.3:
"Waiting list" / "Fair slot allocation" -- confirmed entirely missing by the
whole-system audit this session started from).

Distinct from `QueueToken` (models/queue.py): a queue token is a patient
physically checked in *today*; a `WaitlistEntry` is a patient who wants a
slot with a specific doctor on a specific *future* date, every slot for
which is currently full.

FAIRNESS ALGORITHM: strict FIFO by `created_at` within `(doctor_id,
requested_date)`. When a booked appointment for a doctor is cancelled
(`routers/appointments.py`'s `delete_appointment`, right after
`scheduling_engine.cancel_appointment` succeeds -- see
`try_offer_freed_slot`'s call site), the single OLDEST still-`waiting` entry
for that doctor+date is flipped to `offered`. "Oldest first" is the
fairness rule: whoever asked first gets first refusal on a freed slot,
never a queue-jump based on anything else. This deliberately does NOT
auto-book the offered patient into the freed slot -- the original slot's
exact time may not suit them, and booking on a patient's behalf without any
confirmation step is not appropriate. Staff (or the patient, via a normal
booking call) still books the real appointment; `fulfill_waitlist_entry`
then records which appointment resolved the entry, closing the loop.
"""

import uuid
from datetime import date

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.events import event_publisher
from app.models.appointment import Appointment, WaitlistEntry, WaitlistStatus
from app.models.resource import Doctor
from app.models.user import User
from app.schemas.appointment import WaitlistJoinCreate


def join_waitlist(db: Session, current_user: User, branch_id: uuid.UUID, payload: WaitlistJoinCreate) -> WaitlistEntry:
    """POST /api/v1/waitlist. `branch_id` comes from the caller's token
    (same cross-branch discipline as every other write in this module),
    with the doctor validated to belong to it."""
    doctor = db.get(Doctor, payload.doctor_id)
    if doctor is None or doctor.branch_id != branch_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "doctor not found at this branch")

    entry = WaitlistEntry(
        branch_id=branch_id,
        patient_id=payload.patient_id,
        doctor_id=payload.doctor_id,
        requested_date=payload.requested_date,
        status=WaitlistStatus.waiting,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def list_waitlist(
    db: Session, current_user: User, branch_id: uuid.UUID, doctor_id: uuid.UUID | None
) -> list[WaitlistEntry]:
    """GET /api/v1/waitlist?doctor_id=. Ordered oldest-first -- the same
    FIFO order the fairness algorithm allocates in, so a caller reading this
    list sees exactly who is next."""
    query = select(WaitlistEntry).where(WaitlistEntry.branch_id == branch_id).order_by(WaitlistEntry.created_at)
    if doctor_id is not None:
        query = query.where(WaitlistEntry.doctor_id == doctor_id)
    return list(db.execute(query).scalars().all())


def _get_entry_or_404(db: Session, entry_id: uuid.UUID) -> WaitlistEntry:
    entry = db.get(WaitlistEntry, entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "waitlist entry not found")
    return entry


def fulfill_waitlist_entry(db: Session, current_user: User, entry_id: uuid.UUID, appointment_id: uuid.UUID) -> WaitlistEntry:
    """POST /api/v1/waitlist/{id}/fulfill -- called by staff once the
    waitlisted patient has actually been booked (a normal
    `POST /api/v1/appointments` call), to record which appointment resolved
    this entry and close it out. Not automatic: see module docstring for why
    offering a slot never auto-books it."""
    entry = _get_entry_or_404(db, entry_id)
    if entry.status not in (WaitlistStatus.waiting, WaitlistStatus.offered):
        raise HTTPException(status.HTTP_409_CONFLICT, f"waitlist entry is already {entry.status.value}")

    appointment = db.get(Appointment, appointment_id)
    if appointment is None or appointment.patient_id != entry.patient_id or appointment.doctor_id != entry.doctor_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "appointment_id must reference a real appointment for this entry's own patient and doctor",
        )

    entry.status = WaitlistStatus.fulfilled
    entry.resolved_appointment_id = appointment.id
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def cancel_waitlist_entry(db: Session, current_user: User, entry_id: uuid.UUID) -> None:
    """DELETE /api/v1/waitlist/{id} -- the patient no longer wants this
    slot; frees them from the fairness queue without pretending an
    appointment resolved it."""
    entry = _get_entry_or_404(db, entry_id)
    if entry.status in (WaitlistStatus.fulfilled, WaitlistStatus.cancelled):
        raise HTTPException(status.HTTP_409_CONFLICT, f"waitlist entry is already {entry.status.value}")
    entry.status = WaitlistStatus.cancelled
    db.add(entry)
    db.commit()


def try_offer_freed_slot(db: Session, doctor_id: uuid.UUID, freed_date: date) -> WaitlistEntry | None:
    """Called right after a booked appointment for `doctor_id` is cancelled
    (see routers/appointments.py's `delete_appointment`). Offers the freed
    capacity to the single oldest `waiting` entry for this doctor+date
    (`SELECT ... FOR UPDATE SKIP LOCKED`, same concurrent-safety pattern
    `emergency_engine._select_victim` uses -- two simultaneous cancellations
    for the same doctor/date must never both offer the same waitlist
    position, and neither may block waiting on the other). Returns the
    offered entry, or `None` if nobody is waiting for this doctor+date."""
    entry = db.execute(
        select(WaitlistEntry)
        .where(
            WaitlistEntry.doctor_id == doctor_id,
            WaitlistEntry.requested_date == freed_date,
            WaitlistEntry.status == WaitlistStatus.waiting,
        )
        .order_by(WaitlistEntry.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    ).scalar_one_or_none()
    if entry is None:
        return None

    entry.status = WaitlistStatus.offered
    db.add(entry)
    db.commit()
    db.refresh(entry)

    event_publisher.publish(
        "waitlist.slot_offered",
        {
            "waitlist_entry_id": str(entry.id),
            "patient_id": str(entry.patient_id),
            "doctor_id": str(entry.doctor_id),
            "requested_date": entry.requested_date.isoformat(),
        },
    )
    return entry
