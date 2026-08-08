"""Real-Time Queue module business logic. Routers (routers/queue.py, Phase 2)
stay thin and delegate here -- same "business logic lives in services/,
router is thin" split as services/consultation_service.py,
services/lab_service.py, services/pharmacy_service.py.

Design notes (see PRPs/realtime-queue-prp.md, "MODULE OVERVIEW" / "SERVICE
DESIGN"):

- `QueueToken` has no `patient_id` column of its own (confirmed by reading
  `models/queue.py` in full) -- patient identity is only reachable
  transitively via `appointment_id -> Appointment.patient_id`, when an
  appointment is linked. `check_in` therefore supports two mutually
  exclusive paths, both accepted here as plain fields on `QueueCheckInPayload`
  (the real "exactly one of appointment_id OR (branch_id, department_id)"
  validation is Phase 2's Pydantic schema's job, not this module's):
    * appointment-linked: caller supplies `appointment_id`; `branch_id` is
      derived from `Appointment.branch_id` and `department_id` from
      `Appointment.doctor_id -> Doctor.specialty_id` -- never re-supplied by
      the caller (same "derive, don't trust a duplicate client-supplied
      value" discipline every branch-scoped module this session has used).
    * walk-in: caller supplies `branch_id` + `department_id` directly;
      `appointment_id` is `None` on the created row. This token is
      schema-legal but has no patient linkage at all -- a real, deliberate
      schema limitation (see PRP design decision #1), not an oversight.

- `token_number` is a per-day, per-(branch, department) sequential integer
  computed on the fly (`COUNT(*) WHERE branch_id/department_id match AND
  checked_in_at::date = today`, +1), inside the SAME transaction as the
  INSERT below -- there is no stored "last number" column to race against
  (see PRP design decision #2). A small race window (two simultaneous
  check-ins could rarely compute the same number) is an accepted, documented
  tradeoff for a display-only number with no uniqueness constraint behind
  it -- no extra locking is added for this.

- Status transitions are the small explicit state machine in
  `_LEGAL_TRANSITIONS` below, same discipline as `lab_service.py`'s
  `_TRANSITION_ROLES` / `billing_engine.py`'s `_LEGAL_CLAIM_TRANSITIONS`
  (see PRP design decision #3). `called_at` is set the first time a token
  transitions into `in_consultation`; `completed_at` is set on transition
  into `done`. An illegal transition raises `IllegalTokenStatusTransition`
  (the router maps this to 409 in Phase 2).

- `check_in` and `update_status` are `async def` because both broadcast over
  the existing WebSocket channel (`app.websocket.queue_board.manager`,
  reused as-is -- see PRP design decision #4); every DB call inside them
  stays a plain synchronous SQLAlchemy call on the request's own connection.
  `list_queue` is a plain sync read (no broadcast), like every other read in
  this codebase.

- BROADCAST-WHEN-`department_id`-IS-`None` EDGE CASE: the WebSocket channel
  is topic-scoped per `(branch_id, department_id)` string pair
  (`ConnectionManager._topic`), so there is no single well-known topic a
  `department_id=None` token could broadcast to without inventing a sentinel
  topic string no client is ever told to subscribe to. Per the PRP ("your
  call, document whichever you pick"), this module SKIPS the broadcast
  entirely when `department_id` is `None`, rather than broadcasting to a
  made-up `"none"` topic -- the row is still created/updated and returned to
  the caller normally, it just never reaches the live queue board. In
  practice this should be rare: the walk-in path requires `department_id` to
  be supplied by the caller, and the appointment-linked path always resolves
  one via `Doctor.specialty_id` (`NOT NULL` in the schema) -- the only way to
  reach this branch is a `Doctor` row missing entirely for the appointment's
  `doctor_id` (a data-consistency edge case, not the common path).

- Tenant guard: `QueueToken.branch_id` is a real column, so `authorize()`
  is called directly against the token for `check_in`/`update_status`. For
  `list_queue` (a branch-wide "give me the whole queue" read with no single
  row to check against), a minimal `_BranchScoped` stand-in carries just
  `branch_id` -- same pattern `pharmacy_service.py`'s `_BranchScoped` /
  `get_low_stock`/`get_expiring` use for the same shape of problem. In every
  function, `authorize()` is called before any DB mutation (before
  `db.add`/`db.commit`), per this codebase's established rule (see the
  Pharmacy dispense-function bug this was called out against earlier in the
  session).

- No new ABAC policy registrations exist yet for `resource_type="queue_token"`
  -- those are Phase 2's job in `core/security.py` (`@policy(...)` for
  front_desk/nurse read+write, doctor read-only). Until then, `authorize()`
  will deny every caller for this resource type (no registered policy ->
  denied by default) -- expected and harmless for this phase, since there is
  no router wired up yet to call these functions end-to-end.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import authorize
from app.models.appointment import Appointment, AppointmentStatus
from app.models.queue import QueueToken, TokenStatus
from app.models.resource import Doctor
from app.models.user import User
from app.websocket.queue_board import manager

# Placeholder heuristic constant used by the (Phase 2/3) wait-time consumer's
# recompute -- kept here per the PRP's SERVICE DESIGN block since it's a
# module-level constant of this service, not because check_in/update_status
# use it directly today (see PRP design decision #5).
AVERAGE_CONSULTATION_MINUTES = 15


class IllegalTokenStatusTransition(Exception):
    """Raised when `new_status` is not a legal transition target from a
    token's current status per `_LEGAL_TRANSITIONS` (the router maps this to
    409 in Phase 2)."""


class QueueTokenNotFoundError(Exception):
    """Raised when a `token_id` does not resolve to a `QueueToken` row (the
    router maps this to 404 in Phase 2)."""


class AppointmentNotFoundError(Exception):
    """Raised when `check_in`'s appointment-linked path is given an
    `appointment_id` that does not resolve to an `Appointment` row (the
    router maps this to 404 in Phase 2)."""


_LEGAL_TRANSITIONS: dict[TokenStatus, set[TokenStatus]] = {
    TokenStatus.waiting: {TokenStatus.in_consultation, TokenStatus.delayed, TokenStatus.skipped},
    TokenStatus.delayed: {TokenStatus.waiting, TokenStatus.in_consultation, TokenStatus.skipped},
    TokenStatus.skipped: {TokenStatus.waiting},
    TokenStatus.in_consultation: {TokenStatus.done},
    TokenStatus.done: set(),
}


@dataclass(frozen=True)
class _BranchScoped:
    """Minimal stand-in resource for `authorize()`'s tenant guard when there
    is no single ORM row to check against (`list_queue`'s branch-wide read)
    -- only `branch_id` matters to the guard, mirrors
    `pharmacy_service._BranchScoped`."""

    branch_id: uuid.UUID


@dataclass(frozen=True)
class QueueCheckInPayload:
    """Internal stand-in for the real request shape. Phase 2's Pydantic
    schema (`QueueCheckInRequest`) is responsible for validating "exactly one
    of `appointment_id` OR (`branch_id`, `department_id`)" before this
    function is ever called -- this module only consumes the resolved
    fields, it does not re-validate the "exactly one shape" rule itself."""

    appointment_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None
    department_id: int | None = None
    is_priority: bool = False


def _get_appointment_or_404(db: Session, appointment_id: uuid.UUID) -> Appointment:
    appointment = db.get(Appointment, appointment_id)
    if appointment is None:
        raise AppointmentNotFoundError(f"appointment {appointment_id} not found")
    return appointment


async def check_in(db: Session, current_user: User, payload: QueueCheckInPayload) -> QueueToken:
    """POST /api/v1/queue/check-in (Phase 2). Creates a `QueueToken` either
    from an existing appointment (deriving `branch_id`/`department_id`, never
    trusting duplicate client-supplied values for those) or as a walk-in
    (using the caller-supplied `branch_id`/`department_id` as-is, with
    `appointment_id=None`). See module docstring for both paths and the
    `token_number` race-window tradeoff.
    """
    if payload.appointment_id is not None:
        appointment = _get_appointment_or_404(db, payload.appointment_id)
        branch_id = appointment.branch_id
        appointment_id: uuid.UUID | None = appointment.id
        doctor = db.get(Doctor, appointment.doctor_id)
        department_id = doctor.specialty_id if doctor is not None else None
        # HMS Project Completion Prompt gap ("emergency queue priority"):
        # inherit the appointment's own `is_emergency` flag (set at booking
        # time by emergency_engine.py) so a caller never has to remember to
        # re-flag it here -- still OR-ed with an explicit caller override
        # (payload.is_priority), never AND-ed, so this can only ever ADD
        # priority, never silently drop it.
        is_priority = payload.is_priority or appointment.is_emergency
        # Advance the appointment's OWN status machine on physical check-in --
        # without this, `Appointment.status` never leaves `booked` anywhere
        # in the system (there is no separate "check in an appointment"
        # endpoint), which made `consultation_service.start_consultation`'s
        # `status == in_progress` precondition permanently unreachable
        # through the real app. Only advances forward (`booked` -> checked_in),
        # never regresses a status a different flow may have already moved
        # past (e.g. re-check-in edge cases are out of scope here).
        if appointment.status == AppointmentStatus.booked:
            appointment.status = AppointmentStatus.checked_in
            db.add(appointment)
    else:
        branch_id = payload.branch_id
        department_id = payload.department_id
        appointment_id = None
        is_priority = payload.is_priority

    token = QueueToken(
        branch_id=branch_id,
        appointment_id=appointment_id,
        department_id=department_id,
        token_number=0,  # placeholder -- computed below, before this row is added to the session
        status=TokenStatus.waiting,
        is_priority=is_priority,
    )

    # authorize() before any mutation (no db.add/commit has happened yet) --
    # `token` is a plain in-memory object here, but already carries the real
    # `branch_id` the tenant guard needs.
    authorize(current_user, "queue_token", "write", token)

    today_count = db.execute(
        select(func.count(QueueToken.id)).where(
            QueueToken.branch_id == branch_id,
            QueueToken.department_id == department_id,
            func.date(QueueToken.checked_in_at) == func.current_date(),
        )
    ).scalar_one()
    token.token_number = today_count + 1

    db.add(token)
    db.commit()
    db.refresh(token)

    if department_id is not None:
        await manager.broadcast(
            str(branch_id),
            str(department_id),
            {
                "token_id": str(token.id),
                "status": token.status.value,
                "token_number": token.token_number,
                "checked_in_at": token.checked_in_at.isoformat(),
                "estimated_wait_minutes": token.estimated_wait_minutes,
                "appointment_id": str(token.appointment_id) if token.appointment_id else None,
                "is_priority": token.is_priority,
            },
        )
    # else: department_id is None -- no topic to broadcast to, see module
    # docstring's "BROADCAST-WHEN-department_id-IS-None EDGE CASE".

    return token


async def update_status(
    db: Session, current_user: User, token_id: uuid.UUID, new_status: TokenStatus
) -> QueueToken:
    """PATCH /api/v1/queue/{token_id}/status (Phase 2). Validates the
    transition against `_LEGAL_TRANSITIONS`, stamps `called_at`/`completed_at`
    at the right transitions (PRP design decision #3), commits, and
    broadcasts the update over the live queue-board WebSocket channel.
    """
    token = db.get(QueueToken, token_id)
    if token is None:
        raise QueueTokenNotFoundError(f"queue token {token_id} not found")

    # authorize() before any mutation -- QueueToken.branch_id is a real
    # column, so the tenant guard can check it directly against this row.
    authorize(current_user, "queue_token", "write", token)

    legal_targets = _LEGAL_TRANSITIONS[token.status]
    if new_status not in legal_targets:
        raise IllegalTokenStatusTransition(
            f"cannot transition a queue token from '{token.status.value}' to '{new_status.value}'"
        )

    now = datetime.now(timezone.utc)
    if new_status == TokenStatus.in_consultation:
        # Reachable only from waiting/delayed, and in_consultation's only
        # legal onward transition is -> done (terminal) -- so this branch of
        # the state machine can only ever be entered once per token, meaning
        # called_at is set exactly the first (and only) time.
        token.called_at = now
        # Mirror the check_in()-time advance below: being "called in" is
        # what makes `consultation_service.start_consultation`'s
        # `status == in_progress` precondition true, so a doctor calling a
        # patient in from the Queue Board is what actually unblocks starting
        # the consultation. Walk-in tokens (appointment_id is None) have no
        # appointment to advance.
        if token.appointment_id is not None:
            appointment = db.get(Appointment, token.appointment_id)
            if appointment is not None and appointment.status in (
                AppointmentStatus.booked,
                AppointmentStatus.checked_in,
            ):
                appointment.status = AppointmentStatus.in_progress
                db.add(appointment)
    elif new_status == TokenStatus.done:
        token.completed_at = now

    token.status = new_status
    db.add(token)
    db.commit()
    db.refresh(token)

    if token.department_id is not None:
        await manager.broadcast(
            str(token.branch_id),
            str(token.department_id),
            {
                "token_id": str(token.id),
                "status": token.status.value,
                "token_number": token.token_number,
                "checked_in_at": token.checked_in_at.isoformat(),
                "called_at": token.called_at.isoformat() if token.called_at else None,
                "completed_at": token.completed_at.isoformat() if token.completed_at else None,
                "estimated_wait_minutes": token.estimated_wait_minutes,
                "appointment_id": str(token.appointment_id) if token.appointment_id else None,
                "is_priority": token.is_priority,
            },
        )
    # else: department_id is None -- see module docstring's edge-case note.

    return token


def list_queue(
    db: Session, current_user: User, branch_id: uuid.UUID, department_id: int | None
) -> list[QueueToken]:
    """GET /api/v1/queue/board (Phase 2). Plain sync read, no broadcast --
    the live snapshot for initial page load before any WebSocket message has
    fired. Ordered `is_priority DESC, checked_in_at ASC` -- HMS Project
    Completion Prompt gap ("emergency queue priority"): priority tokens jump
    the line ahead of every non-priority token regardless of arrival time,
    but are still ordered fairly by arrival time AMONG each other (not, say,
    most-recently-flagged-first), and non-priority tokens keep their
    original earliest-arrived-first order below them."""
    authorize(current_user, "queue_token", "read", _BranchScoped(branch_id=branch_id))

    query = select(QueueToken).where(QueueToken.branch_id == branch_id)
    if department_id is not None:
        query = query.where(QueueToken.department_id == department_id)
    query = query.order_by(QueueToken.is_priority.desc(), QueueToken.checked_in_at.asc())

    return list(db.execute(query).scalars().all())
