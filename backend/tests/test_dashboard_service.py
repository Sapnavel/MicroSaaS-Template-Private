"""Tests for the Executive & Operational Dashboard module's service layer
(services/dashboard_service.py), per PRPs/executive-dashboard-prp.md Phase 3.

Runs against a real Postgres (see tests/conftest.py's module docstring) --
every fixture row below is inserted directly via the ORM (no mocking of the
DB), matching test_billing_router.py/test_pharmacy.py's convention of direct
ORM inserts for setup that doesn't need to exercise an HTTP/write endpoint.

HTTP-level role gating and query-param wiring (branch_id/days defaults) are
covered in test_dashboard_router.py; this file is about the aggregate SQL in
each of the five service functions directly.
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.models.appointment import Appointment, AppointmentStatus
from app.models.billing import Invoice, InvoiceStatus
from app.models.pharmacy import InventoryBatch, InventoryItem
from app.models.queue import QueueToken, TokenStatus
from app.models.ward import Bed, BedStatus, Ward
from app.services import dashboard_service

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ward(db, *, branch_id, name="Ward", ward_type="general") -> Ward:
    w = Ward(branch_id=branch_id, name=name, ward_type=ward_type)
    db.add(w)
    db.commit()
    db.refresh(w)
    return w


def _make_bed(db, *, ward_id, status=BedStatus.available, label=None) -> Bed:
    b = Bed(ward_id=ward_id, label=label or f"Bed-{uuid.uuid4().hex[:8]}", status=status)
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


def _make_token(
    db, *, branch_id, department_id=None, checked_in_at, called_at=None, token_number=1
) -> QueueToken:
    t = QueueToken(
        branch_id=branch_id,
        department_id=department_id,
        token_number=token_number,
        status=TokenStatus.done if called_at else TokenStatus.waiting,
        checked_in_at=checked_in_at,
        called_at=called_at,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _make_invoice(db, *, branch_id, patient_id, status, amount, created_at=None) -> Invoice:
    kwargs = dict(branch_id=branch_id, patient_id=patient_id, status=status, total_amount=amount)
    if created_at is not None:
        kwargs["created_at"] = created_at
    inv = Invoice(**kwargs)
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


def _make_appointment(
    db, *, branch_id, patient_id, doctor_id, room_id, status, start, end=None, created_at=None
) -> Appointment:
    end = end or (start + timedelta(minutes=30))
    kwargs = dict(
        branch_id=branch_id,
        patient_id=patient_id,
        doctor_id=doctor_id,
        room_id=room_id,
        time_range=f"[{start.isoformat()},{end.isoformat()})",
        status=status,
    )
    if created_at is not None:
        kwargs["created_at"] = created_at
    appt = Appointment(**kwargs)
    db.add(appt)
    db.commit()
    db.refresh(appt)
    return appt


# ---------------------------------------------------------------------------
# get_occupancy
# ---------------------------------------------------------------------------


def test_occupancy_pct_per_ward(db, branch, system_admin_user):
    ward = _make_ward(db, branch_id=branch.id, name="Ward A")
    _make_bed(db, ward_id=ward.id, status=BedStatus.occupied)
    _make_bed(db, ward_id=ward.id, status=BedStatus.occupied)
    _make_bed(db, ward_id=ward.id, status=BedStatus.available)

    results = dashboard_service.get_occupancy(db, system_admin_user, branch.id)

    row = next(r for r in results if r.ward_id == ward.id)
    assert row.total_beds == 3
    assert row.occupied_beds == 2
    assert row.occupancy_pct == pytest.approx(2 / 3)


def test_occupancy_zero_bed_ward_returns_zero_not_error(db, branch, system_admin_user):
    ward = _make_ward(db, branch_id=branch.id, name="Empty Ward")

    results = dashboard_service.get_occupancy(db, system_admin_user, branch.id)

    row = next(r for r in results if r.ward_id == ward.id)
    assert row.total_beds == 0
    assert row.occupied_beds == 0
    assert row.occupancy_pct == 0.0


def test_occupancy_branch_id_filter(db, branch, other_branch, system_admin_user):
    ward_a = _make_ward(db, branch_id=branch.id, name="Ward A")
    ward_b = _make_ward(db, branch_id=other_branch.id, name="Ward B")

    results = dashboard_service.get_occupancy(db, system_admin_user, branch.id)

    ward_ids = {r.ward_id for r in results}
    assert ward_a.id in ward_ids
    assert ward_b.id not in ward_ids


def test_occupancy_branch_id_none_aggregates_across_branches(db, branch, other_branch, system_admin_user):
    ward_a = _make_ward(db, branch_id=branch.id, name="Ward A")
    ward_b = _make_ward(db, branch_id=other_branch.id, name="Ward B")

    results = dashboard_service.get_occupancy(db, system_admin_user, None)

    ward_ids = {r.ward_id for r in results}
    assert ward_a.id in ward_ids
    assert ward_b.id in ward_ids


# ---------------------------------------------------------------------------
# get_wait_times
# ---------------------------------------------------------------------------


def test_wait_times_empty_table_returns_empty_list_cleanly(db, system_admin_user):
    """`queue_tokens` has zero rows in this deployment (Queue module's
    check-in endpoints were never built, per the PRP's design decision #2).
    This confirms the empty-input path returns `[]` rather than raising."""
    results = dashboard_service.get_wait_times(db, system_admin_user, None, days=7)
    assert results == []


def test_wait_times_only_called_tokens_count(db, branch, specialty, system_admin_user):
    now = datetime.now(timezone.utc)
    _make_token(
        db,
        branch_id=branch.id,
        department_id=specialty.id,
        checked_in_at=now - timedelta(minutes=30),
        called_at=None,
        token_number=1,
    )
    _make_token(
        db,
        branch_id=branch.id,
        department_id=specialty.id,
        checked_in_at=now - timedelta(minutes=40),
        called_at=now - timedelta(minutes=30),
        token_number=2,
    )

    results = dashboard_service.get_wait_times(db, system_admin_user, branch.id, days=7)

    assert len(results) == 1
    assert results[0].sample_size == 1
    assert results[0].avg_wait_minutes == pytest.approx(10.0, abs=0.5)


def test_wait_times_excludes_token_outside_window(db, branch, specialty, system_admin_user):
    now = datetime.now(timezone.utc)
    _make_token(
        db,
        branch_id=branch.id,
        department_id=specialty.id,
        checked_in_at=now - timedelta(days=10),
        called_at=now - timedelta(days=10) + timedelta(minutes=15),
        token_number=1,
    )

    results = dashboard_service.get_wait_times(db, system_admin_user, branch.id, days=7)

    assert results == []


def test_wait_times_avg_computed_correctly(db, branch, specialty, system_admin_user):
    now = datetime.now(timezone.utc)
    _make_token(
        db,
        branch_id=branch.id,
        department_id=specialty.id,
        checked_in_at=now - timedelta(minutes=40),
        called_at=now - timedelta(minutes=30),
        token_number=1,
    )  # 10 min wait
    _make_token(
        db,
        branch_id=branch.id,
        department_id=specialty.id,
        checked_in_at=now - timedelta(minutes=25),
        called_at=now - timedelta(minutes=5),
        token_number=2,
    )  # 20 min wait

    results = dashboard_service.get_wait_times(db, system_admin_user, branch.id, days=7)

    assert len(results) == 1
    assert results[0].sample_size == 2
    assert results[0].avg_wait_minutes == pytest.approx(15.0, abs=0.5)


def test_wait_times_branch_filter_and_none(db, branch, other_branch, specialty, system_admin_user):
    now = datetime.now(timezone.utc)
    _make_token(
        db,
        branch_id=branch.id,
        department_id=specialty.id,
        checked_in_at=now - timedelta(minutes=20),
        called_at=now - timedelta(minutes=10),
        token_number=1,
    )
    _make_token(
        db,
        branch_id=other_branch.id,
        department_id=specialty.id,
        checked_in_at=now - timedelta(minutes=20),
        called_at=now - timedelta(minutes=10),
        token_number=2,
    )

    filtered = dashboard_service.get_wait_times(db, system_admin_user, branch.id, days=7)
    assert {r.branch_id for r in filtered} == {branch.id}

    all_branches = dashboard_service.get_wait_times(db, system_admin_user, None, days=7)
    assert {r.branch_id for r in all_branches} == {branch.id, other_branch.id}


# ---------------------------------------------------------------------------
# get_revenue
# ---------------------------------------------------------------------------


def test_revenue_void_excluded_from_gross_billed_entirely(db, branch, patient, system_admin_user):
    now = datetime.now(timezone.utc)
    _make_invoice(
        db, branch_id=branch.id, patient_id=patient.id, status=InvoiceStatus.void.value,
        amount=Decimal("500.00"), created_at=now,
    )
    _make_invoice(
        db, branch_id=branch.id, patient_id=patient.id, status=InvoiceStatus.paid.value,
        amount=Decimal("100.00"), created_at=now,
    )

    results = dashboard_service.get_revenue(db, system_admin_user, branch.id, days=30)

    assert len(results) == 1
    assert results[0].invoice_count == 1
    assert results[0].gross_billed == pytest.approx(100.0)


def test_revenue_collected_only_sums_paid_status(db, branch, patient, system_admin_user):
    now = datetime.now(timezone.utc)
    _make_invoice(
        db, branch_id=branch.id, patient_id=patient.id, status=InvoiceStatus.open.value,
        amount=Decimal("300.00"), created_at=now,
    )
    _make_invoice(
        db, branch_id=branch.id, patient_id=patient.id, status=InvoiceStatus.paid.value,
        amount=Decimal("200.00"), created_at=now,
    )

    results = dashboard_service.get_revenue(db, system_admin_user, branch.id, days=30)

    assert len(results) == 1
    row = results[0]
    assert row.invoice_count == 2
    assert row.gross_billed == pytest.approx(500.0)
    assert row.collected == pytest.approx(200.0)
    assert row.outstanding == pytest.approx(300.0)


def test_revenue_day_bucketing_groups_correctly(db, branch, patient, system_admin_user):
    day1 = datetime.now(timezone.utc) - timedelta(days=1)
    day2 = datetime.now(timezone.utc) - timedelta(days=3)
    _make_invoice(
        db, branch_id=branch.id, patient_id=patient.id, status=InvoiceStatus.open.value,
        amount=Decimal("50.00"), created_at=day1,
    )
    _make_invoice(
        db, branch_id=branch.id, patient_id=patient.id, status=InvoiceStatus.paid.value,
        amount=Decimal("60.00"), created_at=day1,
    )
    _make_invoice(
        db, branch_id=branch.id, patient_id=patient.id, status=InvoiceStatus.paid.value,
        amount=Decimal("10.00"), created_at=day2,
    )

    results = dashboard_service.get_revenue(db, system_admin_user, branch.id, days=30)

    assert len(results) == 2
    counts = sorted(r.invoice_count for r in results)
    assert counts == [1, 2]


def test_revenue_window_boundary(db, branch, patient, system_admin_user):
    days = 7
    now = datetime.now(timezone.utc)
    inside = now - timedelta(days=days) + timedelta(hours=1)  # just inside the window
    outside = now - timedelta(days=days) - timedelta(hours=1)  # just outside the window

    _make_invoice(
        db, branch_id=branch.id, patient_id=patient.id, status=InvoiceStatus.paid.value,
        amount=Decimal("70.00"), created_at=inside,
    )
    _make_invoice(
        db, branch_id=branch.id, patient_id=patient.id, status=InvoiceStatus.paid.value,
        amount=Decimal("999.00"), created_at=outside,
    )

    results = dashboard_service.get_revenue(db, system_admin_user, branch.id, days=days)

    total_gross = sum(r.gross_billed for r in results)
    assert total_gross == pytest.approx(70.0)


# ---------------------------------------------------------------------------
# get_no_show_rate
# ---------------------------------------------------------------------------


def test_no_show_rate_excludes_cancelled_and_preempted_from_total_considered(
    db, branch, doctor_record, room, patient, system_admin_user
):
    start = datetime.now(timezone.utc) - timedelta(days=1)
    # `completed` participates in the appointment EXCLUDE constraint (active
    # status); `no_show`/`cancelled`/`preempted` are excluded from it (see
    # models/appointment.py's WHERE clause), so all four can safely share the
    # same doctor/room/time_range without a DB conflict.
    _make_appointment(
        db, branch_id=branch.id, patient_id=patient.id, doctor_id=doctor_record.id,
        room_id=room.id, status=AppointmentStatus.completed, start=start,
    )
    _make_appointment(
        db, branch_id=branch.id, patient_id=patient.id, doctor_id=doctor_record.id,
        room_id=room.id, status=AppointmentStatus.no_show, start=start,
    )
    _make_appointment(
        db, branch_id=branch.id, patient_id=patient.id, doctor_id=doctor_record.id,
        room_id=room.id, status=AppointmentStatus.cancelled, start=start,
    )
    _make_appointment(
        db, branch_id=branch.id, patient_id=patient.id, doctor_id=doctor_record.id,
        room_id=room.id, status=AppointmentStatus.preempted, start=start,
    )

    results = dashboard_service.get_no_show_rate(db, system_admin_user, branch.id, days=30)

    assert len(results) == 1
    row = results[0]
    assert row.total_considered == 2  # completed + no_show only
    assert row.no_show_count == 1
    assert row.no_show_rate == pytest.approx(0.5)


def test_no_show_rate_preempted_does_not_inflate_total_considered(
    db, branch, doctor_record, room, patient, system_admin_user
):
    """CRITICAL fix confirmation: `preempted` was bumped by the system for a
    higher-priority emergency, not a patient no-show -- it must be excluded
    from the denominator entirely, same as `cancelled`."""
    start = datetime.now(timezone.utc) - timedelta(days=1)
    _make_appointment(
        db, branch_id=branch.id, patient_id=patient.id, doctor_id=doctor_record.id,
        room_id=room.id, status=AppointmentStatus.completed, start=start,
    )
    _make_appointment(
        db, branch_id=branch.id, patient_id=patient.id, doctor_id=doctor_record.id,
        room_id=room.id, status=AppointmentStatus.preempted, start=start,
    )

    results = dashboard_service.get_no_show_rate(db, system_admin_user, branch.id, days=30)

    assert len(results) == 1
    assert results[0].total_considered == 1  # only the completed appointment
    assert results[0].no_show_count == 0


def test_no_show_rate_zero_considered_returns_empty_list_not_error(
    db, branch, doctor_record, room, patient, system_admin_user
):
    """When every appointment in the window is `cancelled`/`preempted` (or
    there are none at all), the underlying `GROUP BY branch_id` query
    produces zero matching rows for that branch entirely -- Postgres never
    emits a group with no member rows -- so `get_no_show_rate` returns `[]`
    cleanly rather than a row with `total_considered=0`. This confirms the
    no-crash path; `NoShowRateResult`'s own `no_show_rate = 0.0 if
    total_considered == 0 else ...` guard is defense-in-depth for a query
    shape change, not reachable via this exact SQL today."""
    start = datetime.now(timezone.utc) - timedelta(days=1)
    _make_appointment(
        db, branch_id=branch.id, patient_id=patient.id, doctor_id=doctor_record.id,
        room_id=room.id, status=AppointmentStatus.cancelled, start=start,
    )
    _make_appointment(
        db, branch_id=branch.id, patient_id=patient.id, doctor_id=doctor_record.id,
        room_id=room.id, status=AppointmentStatus.preempted, start=start,
    )

    results = dashboard_service.get_no_show_rate(db, system_admin_user, branch.id, days=30)

    assert results == []


def test_no_show_rate_window_uses_time_range_lower_bound_not_created_at(
    db, branch, doctor_record, other_doctor_record, room, patient, system_admin_user
):
    """Pins down which timestamp actually governs the window: the query must
    use `func.lower(Appointment.time_range)`, not `created_at`."""
    now = datetime.now(timezone.utc)

    # time_range OUTSIDE the 30-day window, but created_at freshly "now"
    # (inside the window) -- must be EXCLUDED.
    outside_start = now - timedelta(days=100)
    _make_appointment(
        db, branch_id=branch.id, patient_id=patient.id, doctor_id=doctor_record.id,
        room_id=room.id, status=AppointmentStatus.completed, start=outside_start,
        created_at=now,
    )

    # time_range INSIDE the window, but created_at set far in the past
    # (outside the window) -- must be INCLUDED.
    inside_start = now - timedelta(days=5)
    _make_appointment(
        db, branch_id=branch.id, patient_id=patient.id, doctor_id=other_doctor_record.id,
        room_id=room.id, status=AppointmentStatus.completed, start=inside_start,
        created_at=now - timedelta(days=100),
    )

    results = dashboard_service.get_no_show_rate(db, system_admin_user, branch.id, days=30)

    assert len(results) == 1
    assert results[0].total_considered == 1


# ---------------------------------------------------------------------------
# get_stock_alerts
# ---------------------------------------------------------------------------


def test_stock_alerts_call_through_bundles_low_stock_and_expiring(
    db, branch, drug, other_drug, system_admin_user
):
    """Confirms `get_stock_alerts` really is a thin call-through to
    `pharmacy_service.get_low_stock`/`get_expiring` -- a low-stock item and
    an expiring batch (built via the SAME fixture shapes pharmacy's own
    tests use) both appear correctly in the bundled response."""
    low_stock_item = InventoryItem(branch_id=branch.id, drug_id=drug.id, reorder_threshold=100)
    db.add(low_stock_item)
    db.commit()
    db.refresh(low_stock_item)
    db.add(
        InventoryBatch(
            inventory_item_id=low_stock_item.id, batch_number="B1", quantity=5,
            expiry_date=date.today() + timedelta(days=365),
        )
    )
    db.commit()

    # Separate drug for the expiring-soon batch so it doesn't ALSO trip
    # low-stock -- keeps the two lists cleanly distinguishable below.
    expiring_item = InventoryItem(branch_id=branch.id, drug_id=other_drug.id, reorder_threshold=0)
    db.add(expiring_item)
    db.commit()
    db.refresh(expiring_item)
    expiring_batch = InventoryBatch(
        inventory_item_id=expiring_item.id, batch_number="B2", quantity=50,
        expiry_date=date.today() + timedelta(days=5),
    )
    db.add(expiring_batch)
    db.commit()
    db.refresh(expiring_batch)

    result = dashboard_service.get_stock_alerts(db, system_admin_user, branch.id)

    low_stock_drug_ids = {r.drug_id for r in result.low_stock}
    expiring_batch_ids = {r.batch_id for r in result.expiring_soon}
    assert drug.id in low_stock_drug_ids
    assert expiring_batch.id in expiring_batch_ids
