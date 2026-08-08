"""Unified Patient Timeline (HMS Project Completion Prompt gap: "Patient
medical timeline" -- a patient's history was only ever reachable piecemeal,
one module's own list endpoint at a time (their appointments, their
consultations, their lab orders, ... each a separate round trip with no
combined chronological view). This module is a pure read-side aggregator: it
queries each source table directly (not through each module's own service
function) since none of those functions' RBAC/response-shaping is needed
here -- ownership/role gating happens once, at this module's own two call
sites (`routers/patients.py`'s staff endpoint, `routers/patient_portal.py`'s
`/me/timeline`), not per-source-table.

Deliberately NOT reusing `patient_service.shape_patient_response`'s
front_desk/billing_admin "demographics-only" split: this module's staff
entry point is gated to clinical roles only (doctor/nurse/system_admin) by
`routers/patients.py`'s role list, not exposed to front_desk/billing_admin at
all -- simpler and safer than inventing a second, timeline-specific
redaction scheme for two roles that already have narrower read access to
this patient's record everywhere else in the codebase.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.appointment import Appointment
from app.models.billing import Invoice
from app.models.consultation import Consultation, Prescription
from app.models.lab import LabOrder
from app.models.ward import Admission


class TimelineEvent(BaseModel):
    """One entry in a patient's unified timeline. `event_type` is a fixed
    string tag (`"appointment"`, `"consultation"`, `"lab_order"`,
    `"prescription"`, `"admission"`, `"invoice"`), not a `Literal`-typed
    enum on the wire -- new event types can be added here without a
    frontend contract break (an unrecognized tag is simply rendered
    generically, same "additive, forward-compatible" reasoning
    `notification_engine.py`'s `NotificationTemplate` uses for template
    names)."""

    event_type: str
    id: uuid.UUID
    occurred_at: datetime
    summary: str


def _lower_bound(range_value) -> datetime | None:
    """Same defensive `.lower`-can-be-a-string normalization
    `scheduling_engine._range_bounds` uses, trimmed to just the lower bound
    (all this module needs for sorting)."""
    lower = range_value.lower
    if lower is None:
        return None
    if isinstance(lower, str):
        return datetime.fromisoformat(lower)
    return lower


def get_patient_timeline(db: Session, patient_id: uuid.UUID) -> list[TimelineEvent]:
    """Builds the full, merged, most-recent-first timeline for one patient.
    Callers are responsible for their own authorization BEFORE calling this
    (see module docstring) -- this function itself does not call
    `core.security.authorize()`, it is a pure read across six tables all
    pre-filtered to `patient_id`."""
    events: list[TimelineEvent] = []

    appointments = db.execute(select(Appointment).where(Appointment.patient_id == patient_id)).scalars().all()
    for appt in appointments:
        occurred_at = _lower_bound(appt.time_range)
        if occurred_at is None:
            continue
        events.append(
            TimelineEvent(
                event_type="appointment",
                id=appt.id,
                occurred_at=occurred_at,
                summary=f"Appointment ({appt.status.value})",
            )
        )

    consultations = (
        db.execute(select(Consultation).where(Consultation.patient_id == patient_id)).scalars().all()
    )
    for consultation in consultations:
        events.append(
            TimelineEvent(
                event_type="consultation",
                id=consultation.id,
                occurred_at=consultation.started_at,
                summary=f"Consultation: {consultation.symptoms}",
            )
        )

    lab_orders = db.execute(select(LabOrder).where(LabOrder.patient_id == patient_id)).scalars().all()
    for lab_order in lab_orders:
        events.append(
            TimelineEvent(
                event_type="lab_order",
                id=lab_order.id,
                occurred_at=lab_order.created_at,
                summary=f"Lab order: {lab_order.test_code} ({lab_order.status.value})",
            )
        )

    prescriptions = (
        db.execute(select(Prescription).where(Prescription.patient_id == patient_id)).scalars().all()
    )
    for prescription in prescriptions:
        events.append(
            TimelineEvent(
                event_type="prescription",
                id=prescription.id,
                occurred_at=prescription.created_at,
                summary=f"Prescription ({prescription.status})",
            )
        )

    admissions = db.execute(select(Admission).where(Admission.patient_id == patient_id)).scalars().all()
    for admission in admissions:
        occurred_at = _lower_bound(admission.stay_range)
        if occurred_at is None:
            continue
        status_label = "discharged" if admission.discharged_at is not None else "admitted"
        events.append(
            TimelineEvent(
                event_type="admission",
                id=admission.id,
                occurred_at=occurred_at,
                summary=f"Ward admission ({status_label})",
            )
        )

    invoices = db.execute(select(Invoice).where(Invoice.patient_id == patient_id)).scalars().all()
    for invoice in invoices:
        events.append(
            TimelineEvent(
                event_type="invoice",
                id=invoice.id,
                occurred_at=invoice.created_at,
                summary=f"Invoice {invoice.total_amount} ({invoice.status})",
            )
        )

    events.sort(key=lambda event: event.occurred_at, reverse=True)
    return events
