from sqlalchemy import Boolean, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TriageLevel(Base):
    __tablename__ = "triage_levels"

    level: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    label: Mapped[str] = mapped_column(String, nullable=False)
    max_wait_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    preempts: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
