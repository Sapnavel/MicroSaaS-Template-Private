import enum
import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Numeric, SmallInteger, String, func
from sqlalchemy.dialects.postgresql import TSTZRANGE, UUID, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class AppointmentStatus(str, enum.Enum):
    booked = "booked"
    checked_in = "checked_in"
    in_progress = "in_progress"
    completed = "completed"
    cancelled = "cancelled"
    no_show = "no_show"
    preempted = "preempted"


class RecurrenceFrequency(str, enum.Enum):
    daily = "daily"
    weekly = "weekly"
    biweekly = "biweekly"


class WaitlistStatus(str, enum.Enum):
    waiting = "waiting"
    offered = "offered"
    fulfilled = "fulfilled"
    cancelled = "cancelled"


class AppointmentSeries(Base, UUIDPrimaryKeyMixin):
    """A recurring-appointment request (HMS Project Completion Prompt,
    "Repeat or recurring appointments"). Each occurrence is booked as its
    own independent `Appointment` row (`Appointment.series_id` links back
    here) through the exact same `scheduling_engine.book_appointment` path
    every one-off booking uses -- there is no separate, weaker conflict-
    check code path for recurring bookings. See
    `services/recurring_appointment_service.py`."""

    __tablename__ = "appointment_series"

    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)
    doctor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("doctors.id"), nullable=False)
    frequency: Mapped[str] = mapped_column(String, nullable=False)
    occurrences: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class WaitlistEntry(Base, UUIDPrimaryKeyMixin):
    """A patient waiting for a FUTURE slot with a specific doctor to open up
    on a given date -- distinct from `QueueToken` (models/queue.py), which
    is a patient physically checked in today. Fairness rule: FIFO by
    `created_at` within `(doctor_id, requested_date)` -- see
    `services/waitlist_service.py`'s module docstring for the full
    allocation algorithm."""

    __tablename__ = "appointment_waitlist_entries"

    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)
    doctor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("doctors.id"), nullable=False)
    requested_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[WaitlistStatus] = mapped_column(
        Enum(WaitlistStatus, name="waitlist_status"),
        nullable=False,
        default=WaitlistStatus.waiting,
    )
    resolved_appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointments.id")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Appointment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """The concurrency-critical table.

    The EXCLUDE constraint is the authoritative guard against double-booking
    a doctor: Postgres physically rejects an INSERT/UPDATE whose time_range
    overlaps another active (non-cancelled/no_show/preempted) appointment
    for the same doctor_id. See docs/ARCHITECTURE.md section 5 for why this
    is layered underneath the Redis lock rather than relied on alone.
    """

    __tablename__ = "appointments"
    __table_args__ = (
        ExcludeConstraint(
            ("doctor_id", "="),
            ("time_range", "&&"),
            where="status NOT IN ('cancelled', 'no_show', 'preempted')",
            using="gist",
        ),
    )

    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)
    doctor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("doctors.id"), nullable=False)
    room_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("rooms.id"), nullable=False)
    time_range = mapped_column(TSTZRANGE, nullable=False)
    status: Mapped[AppointmentStatus] = mapped_column(
        Enum(AppointmentStatus, name="appointment_status"),
        nullable=False,
        default=AppointmentStatus.booked,
    )
    triage_level: Mapped[int | None] = mapped_column(SmallInteger, ForeignKey("triage_levels.level"))
    is_emergency: Mapped[bool] = mapped_column(default=False)
    no_show_risk_score: Mapped[float | None] = mapped_column(Numeric(4, 3))
    preempted_by_appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointments.id")
    )
    reschedule_of_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("appointments.id"))
    series_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("appointment_series.id"))


class AppointmentRoomLock(Base):
    __tablename__ = "appointment_room_locks"
    __table_args__ = (ExcludeConstraint(("room_id", "="), ("time_range", "&&"), using="gist"),)

    appointment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointments.id", ondelete="CASCADE"), primary_key=True
    )
    room_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("rooms.id"), nullable=False)
    time_range = mapped_column(TSTZRANGE, nullable=False)


class AppointmentEquipmentLock(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "appointment_equipment_locks"
    __table_args__ = (ExcludeConstraint(("equipment_id", "="), ("time_range", "&&"), using="gist"),)

    appointment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointments.id", ondelete="CASCADE"), nullable=False
    )
    equipment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("equipment.id"), nullable=False)
    time_range = mapped_column(TSTZRANGE, nullable=False)
