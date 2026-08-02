import uuid

from sqlalchemy import Boolean, ForeignKey, SmallInteger, String
from sqlalchemy.dialects.postgresql import TSTZRANGE, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import UUIDPrimaryKeyMixin


class Specialty(Base):
    __tablename__ = "specialties"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)


class Doctor(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "doctors"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), unique=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False)
    specialty_id: Mapped[int] = mapped_column(SmallInteger, ForeignKey("specialties.id"), nullable=False)
    default_consult_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=15)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class DoctorShift(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "doctor_shifts"

    doctor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("doctors.id"), nullable=False)
    shift_range = mapped_column(TSTZRANGE, nullable=False)


class Room(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "rooms"

    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    room_type: Mapped[str] = mapped_column(String, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Equipment(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "equipment"

    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    is_mobile: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
