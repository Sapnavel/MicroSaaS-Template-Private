import enum
import uuid

from sqlalchemy import Enum, ForeignKey, Numeric, SmallInteger
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
