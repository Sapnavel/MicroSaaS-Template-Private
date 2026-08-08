"""Tests for the wait-time-recalculation producer wired into
`consultation_service.complete_consultation` (HMS Project Completion Prompt
gap: `scheduling_engine.recalculate_downstream_wait_times` existed with zero
call sites -- the consumer half of the pipeline
(`workers/queue_wait_time_consumer.py`) was real, but nothing ever produced
its input event, so a doctor running over never actually recalculated
anything downstream).

Uses `monkeypatch` on `consultation_service.scheduling_engine.
recalculate_downstream_wait_times` rather than a real RabbitMQ round-trip --
this is a pure "was the producer called with the right arguments, under the
right condition" test, not a message-broker integration test. Goes through
the real HTTP router (`PATCH /api/v1/consultations/{id}/complete`), not a
direct service-function call, so `current_user.doctor_id` is stashed by
`get_current_user` the same way it is in production (see
core/security.py's docstring -- a direct service call with a hand-built
`User` object would leave that non-mapped attribute unset and 403 the
doctor-ownership check for no real reason)."""

from datetime import datetime, timedelta, timezone

from app.models.appointment import Appointment, AppointmentStatus
from app.models.consultation import Consultation
from app.services import consultation_service


def _login(client, email: str, password: str) -> str:
    resp = client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_appointment(db, *, branch_id, doctor_id, room_id, patient_id, start, end) -> Appointment:
    appt = Appointment(
        branch_id=branch_id,
        patient_id=patient_id,
        doctor_id=doctor_id,
        room_id=room_id,
        time_range=f"[{start.isoformat()},{end.isoformat()})",
        status=AppointmentStatus.in_progress,
    )
    db.add(appt)
    db.commit()
    db.refresh(appt)
    return appt


def _make_consultation(db, appointment: Appointment) -> Consultation:
    c = Consultation(
        appointment_id=appointment.id,
        doctor_id=appointment.doctor_id,
        patient_id=appointment.patient_id,
        symptoms="Test symptoms",
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def test_complete_consultation_running_over_triggers_recalc(
    monkeypatch, client, db, staff_user, staff_password, branch, doctor_record, room, patient
):
    now = datetime.now(timezone.utc)
    # Scheduled to have ended 5 minutes ago -- completing it "now" means the
    # doctor ran over.
    appointment = _make_appointment(
        db,
        branch_id=branch.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        patient_id=patient.id,
        start=now - timedelta(minutes=25),
        end=now - timedelta(minutes=5),
    )
    consultation = _make_consultation(db, appointment)

    calls = []
    monkeypatch.setattr(
        consultation_service.scheduling_engine,
        "recalculate_downstream_wait_times",
        lambda db_, doctor_id, actual_end_time: calls.append((doctor_id, actual_end_time)),
    )

    token = _login(client, staff_user.email, staff_password)
    resp = client.patch(
        f"/api/v1/consultations/{consultation.id}/complete", json={}, headers=_auth(token)
    )
    assert resp.status_code == 200, resp.text

    assert len(calls) == 1
    called_doctor_id, called_end_time = calls[0]
    assert called_doctor_id == doctor_record.id
    assert called_end_time is not None


def test_complete_consultation_on_time_does_not_trigger_recalc(
    monkeypatch, client, db, staff_user, staff_password, branch, doctor_record, room, patient
):
    now = datetime.now(timezone.utc)
    # Scheduled to end well in the future -- completing "now" means the
    # doctor finished early, not over.
    appointment = _make_appointment(
        db,
        branch_id=branch.id,
        doctor_id=doctor_record.id,
        room_id=room.id,
        patient_id=patient.id,
        start=now - timedelta(minutes=5),
        end=now + timedelta(hours=1),
    )
    consultation = _make_consultation(db, appointment)

    calls = []
    monkeypatch.setattr(
        consultation_service.scheduling_engine,
        "recalculate_downstream_wait_times",
        lambda db_, doctor_id, actual_end_time: calls.append((doctor_id, actual_end_time)),
    )

    token = _login(client, staff_user.email, staff_password)
    resp = client.patch(
        f"/api/v1/consultations/{consultation.id}/complete", json={}, headers=_auth(token)
    )
    assert resp.status_code == 200, resp.text

    assert calls == []
