from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def set_tenant_context(db: Session, branch_id: str) -> None:
    """Set the RLS session variable for the current transaction.

    Must be called at the start of every request-scoped transaction so that
    Postgres Row-Level Security (see database/schema.sql section 13) enforces
    branch isolation even if application-layer filtering has a bug.
    """
    db.execute(text("SET LOCAL app.current_branch_id = :branch_id"), {"branch_id": branch_id})
