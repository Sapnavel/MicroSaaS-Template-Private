"""ORM models for the Ward / Bed / OT module (database/schema.sql §10:
`wards`, `beds`, `admissions`, `ot_schedules`), PRPs/ward-bed-ot-module-prp.md
Phase 1.

This module is the ward/bed/OT structural cousin of `models/appointment.py`:
a resource id plus a `TSTZRANGE`, protected by a Postgres `EXCLUDE USING
gist` constraint, the exact `ExcludeConstraint` pattern copied from
`Appointment` (see that file's docstring for why this is layered underneath
the Redis lock in `core/locking.py` rather than relied on alone -- the actual
booking engine lives in `services/ward_engine.py`, Phase 1, not here).

Key design decisions carried over verbatim from the PRP (read there for the
full reasoning, this is the short version so this file's shape makes sense
on its own):

1. `Admission.stay_range` is created OPEN-ENDED (`[start_time,)`) at admit
   time -- you don't know the discharge time yet. Discharge doesn't delete
   or replace the row, it `UPDATE`s `stay_range` to close the upper bound.
   Both operations live in `services/ward_engine.py`; this file only
   declares the column as `TSTZRANGE NOT NULL`, same as `Appointment.time_range`.

2. `BedStatus` has a real 4-state lifecycle (`available -> occupied ->
   cleaning -> available`, with `blocked` an orthogonal manual override
   reachable from `available` or `cleaning`). `occupied` is never a legal
   source or target of the manual `set_bed_status` transition function --
   it is only ever set by a successful `admit_patient` / cleared by
   `discharge_patient`'s en-route-to-`cleaning` transition, in
   `services/ward_engine.py`.

3. `OTSchedule`'s `EXCLUDE` constraint covers `room_id` only, matching
   schema.sql exactly -- there is deliberately NO `surgeon_id` exclusion
   here (see `services/ward_engine.py`'s `schedule_ot` docstring for why:
   Postgres can't express a single exclusion constraint spanning this table
   and `appointments`, so surgeon-conflict prevention for OT bookings is
   app-level-only, a real and documented gap, not an oversight).

`BedStatus` genuinely IS a Postgres `CREATE TYPE ... AS ENUM` (schema.sql
§10: `CREATE TYPE bed_status AS ENUM ('available', 'occupied', 'cleaning',
'blocked')`) -- confirmed by reading the actual DDL, not assumed from a
similarly-named column elsewhere (the same "verify, don't guess" discipline
`LabOrderStatus`'s docstring in `models/lab.py` calls out). `Ward.ward_type`,
by contrast, is plain `TEXT` in schema.sql (just a comment listing example
values `'icu'`/`'general'`/`'private'`, no `CREATE TYPE`) -- mapped as a
plain `String`, not an `Enum`.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import TSTZRANGE, UUID, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import UUIDPrimaryKeyMixin


class Ward(Base, UUIDPrimaryKeyMixin):
    """A physical ward within a branch (schema.sql: `branch_id`, `name`,
    `ward_type` -- all plain columns, no exclusion/uniqueness constraints of
    its own). `ward_type` is plain TEXT (e.g. 'icu', 'general', 'private'),
    not a Postgres enum -- see module docstring."""

    __tablename__ = "wards"

    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    ward_type: Mapped[str] = mapped_column(String, nullable=False)


class BedStatus(str, enum.Enum):
    """Mirrors Postgres `bed_status` (schema.sql §10: `CREATE TYPE bed_status
    AS ENUM ('available', 'occupied', 'cleaning', 'blocked')`). `str` mixin
    (same pattern as `AppointmentStatus`/`LabOrderStatus`) so a status
    serializes as its plain string value without a custom encoder.

    See module docstring point 2 for the lifecycle this enum drives:
    `available -> occupied` (admit) `-> cleaning` (discharge) `->
    available` (explicit "mark clean"), with `blocked` an orthogonal manual
    override reachable from `available` or `cleaning`. `occupied` is only
    ever set/cleared by `services/ward_engine.py`'s `admit_patient` /
    `discharge_patient` -- never by the manual `set_bed_status` transition
    function.
    """

    available = "available"
    occupied = "occupied"
    cleaning = "cleaning"
    blocked = "blocked"


class Bed(Base, UUIDPrimaryKeyMixin):
    """A physical bed within a `Ward`. `UniqueConstraint("ward_id", "label")`
    mirrors schema.sql's `UNIQUE (ward_id, label)` exactly -- two beds in the
    same ward cannot share a label, but the same label may repeat across
    different wards (e.g. every ward having a "Bed 1")."""

    __tablename__ = "beds"
    __table_args__ = (UniqueConstraint("ward_id", "label"),)

    ward_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("wards.id"), nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[BedStatus] = mapped_column(
        Enum(BedStatus, name="bed_status"),
        nullable=False,
        default=BedStatus.available,
    )


class Admission(Base, UUIDPrimaryKeyMixin):
    """One patient's stay in one bed. `stay_range` is `TSTZRANGE NOT NULL`,
    created open-ended (`[start_time,)`) by `services/ward_engine.py`'s
    `admit_patient` and closed (`[start_time, discharge_time)`) by
    `discharge_patient` -- see module docstring point 1. This row is never
    deleted or replaced across that lifecycle, only `UPDATE`d.

    The `EXCLUDE USING gist (bed_id WITH =, stay_range WITH &&)` constraint
    is copied exactly from `Appointment`'s `__table_args__` pattern (see
    `models/appointment.py`) and is the authoritative guard against two
    overlapping admissions for the same physical bed -- the `Bed.status`
    fast-path check in `admit_patient` is a UX nicety layered on top, not a
    substitute for this constraint (same two-layer reasoning
    `scheduling_engine.py` documents for `Appointment`).
    """

    __tablename__ = "admissions"
    __table_args__ = (ExcludeConstraint(("bed_id", "="), ("stay_range", "&&"), using="gist"),)

    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)
    bed_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("beds.id"), nullable=False)
    stay_range = mapped_column(TSTZRANGE, nullable=False)
    admitted_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    discharged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OTSchedule(Base, UUIDPrimaryKeyMixin):
    """One Operation Theatre booking: a `Room` (schema.sql comment: room_type
    = 'ot'), a `Patient`, a `Doctor` acting as surgeon, and a `time_range`.

    The `EXCLUDE USING gist (room_id WITH =, time_range WITH &&)` constraint
    is room-only, matching schema.sql exactly -- see module docstring point
    3 and `services/ward_engine.py`'s `schedule_ot` docstring for why there
    is deliberately no equivalent DB-level guard for `surgeon_id` here.
    """

    __tablename__ = "ot_schedules"
    __table_args__ = (ExcludeConstraint(("room_id", "="), ("time_range", "&&"), using="gist"),)

    room_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("rooms.id"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)
    surgeon_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("doctors.id"), nullable=False)
    time_range = mapped_column(TSTZRANGE, nullable=False)
