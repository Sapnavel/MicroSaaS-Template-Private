import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.database import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    branch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String, nullable=False)
    resource_type: Mapped[str] = mapped_column(String, nullable=False)
    resource_id: Mapped[str] = mapped_column(String, nullable=False)
    event_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    prev_hash: Mapped[str | None] = mapped_column(String)
    row_hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def record_audit_event(
    db: Session,
    *,
    branch_id: uuid.UUID | None,
    actor_user_id: uuid.UUID | None,
    action: str,
    resource_type: str,
    resource_id: str,
    metadata: dict[str, Any] | None = None,
) -> AuditLog:
    """Append an immutable, hash-chained audit event.

    The DB additionally revokes UPDATE/DELETE on audit_logs (see schema.sql)
    so this function is the only way rows get created, and no code path can
    alter them afterward.
    """
    metadata = metadata or {}
    last = db.query(AuditLog).order_by(AuditLog.id.desc()).first()
    prev_hash = last.row_hash if last else ""
    payload = json.dumps(
        {
            "action": action,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "metadata": metadata,
            "prev_hash": prev_hash,
        },
        sort_keys=True,
        default=str,
    )
    row_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    entry = AuditLog(
        branch_id=branch_id,
        actor_user_id=actor_user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        event_metadata=metadata,
        prev_hash=prev_hash,
        row_hash=row_hash,
    )
    db.add(entry)
    return entry
