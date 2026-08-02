import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, SmallInteger, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import UUIDPrimaryKeyMixin


class TokenStatus(str, enum.Enum):
    waiting = "waiting"
    in_consultation = "in_consultation"
    delayed = "delayed"
    skipped = "skipped"
    done = "done"


class QueueToken(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "queue_tokens"

    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False)
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("appointments.id"))
    department_id: Mapped[int | None] = mapped_column(SmallInteger, ForeignKey("specialties.id"))
    token_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[TokenStatus] = mapped_column(Enum(TokenStatus, name="token_status"), default=TokenStatus.waiting)
    checked_in_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    called_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    estimated_wait_minutes: Mapped[int | None] = mapped_column(SmallInteger)
