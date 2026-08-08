"""Automated proof that `POST /api/v1/appointments` stays correct under
truly simultaneous requests for the same doctor/room/slot (HMS Project
Completion Prompt, section 5: "Add an automated concurrency test that sends
multiple simultaneous requests for the same slot. The test must prove that
no conflicting appointments are created.").

Manual verification of this exact guarantee was already run once against a
live docker-compose stack this session (5 concurrent `curl` requests -> one
201, four 409, confirmed via direct `psql` row count). This file automates
that same proof so it runs in CI/`pytest` rather than depending on a human
re-running curl by hand.

INFRA NOTE: the shared `client` fixture (conftest.py) binds every request to
one pre-opened `db` session, which is convenient for sequential tests but
would serialize "concurrent" requests onto a single non-thread-safe
Session -- defeating the point of this test. This file instead overrides
`get_db` with a factory that hands each request its own fresh `Session`
(exactly what `app.database.get_db` does in production), so N real OS
threads each drive an independent DB connection through the full HTTP
stack -- router -> scheduling_engine.book_appointment -> Redis lock ->
Postgres EXCLUDE constraint -- the same code path production traffic uses.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.database import get_db
from app.main import app
from tests.conftest import TestSessionLocal

CONCURRENT_REQUESTS = 8


def _login(client: TestClient, email: str, password: str) -> str:
    resp = client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def test_concurrent_booking_requests_yield_exactly_one_success(
    db, redis_client, branch, doctor_record, room, patient, front_desk_user, staff_password
):
    def _per_request_db():
        session = TestSessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _per_request_db
    try:
        with TestClient(app) as client:
            token = _login(client, front_desk_user.email, staff_password)
            headers = {"Authorization": f"Bearer {token}"}

            start = datetime.now(timezone.utc) + timedelta(days=1)
            payload = {
                "patient_id": str(patient.id),
                "doctor_id": str(doctor_record.id),
                "room_id": str(room.id),
                "start_time": start.isoformat(),
                "duration_minutes": 20,
            }

            # A barrier lines every worker thread up so the requests actually
            # fire together, rather than trickling out as ThreadPoolExecutor
            # schedules them one at a time.
            barrier = threading.Barrier(CONCURRENT_REQUESTS)

            def fire(_: int):
                barrier.wait()
                return client.post("/api/v1/appointments", json=payload, headers=headers)

            with ThreadPoolExecutor(max_workers=CONCURRENT_REQUESTS) as executor:
                responses = list(executor.map(fire, range(CONCURRENT_REQUESTS)))

            statuses = [r.status_code for r in responses]
            # Every response must be a definitive success or a definitive
            # "someone else has this slot" -- never a 500, never a silent
            # double-insert.
            assert all(status in (201, 409, 423) for status in statuses), statuses
            assert statuses.count(201) == 1, f"expected exactly one 201, got statuses={statuses}"
    finally:
        app.dependency_overrides[get_db] = lambda: db

    # Authoritative DB-level check, independent of what the API claimed:
    # exactly one non-cancelled appointment exists for this doctor at all,
    # since every request in this test targeted the identical slot.
    count = db.execute(
        text(
            "SELECT COUNT(*) FROM appointments "
            "WHERE doctor_id = :doctor_id AND status NOT IN ('cancelled', 'no_show', 'preempted')"
        ),
        {"doctor_id": str(doctor_record.id)},
    ).scalar_one()
    assert count == 1, f"expected exactly 1 appointment row, found {count}"


def test_concurrent_booking_second_wave_after_cancellation_succeeds(
    db, redis_client, branch, doctor_record, room, patient, front_desk_user, staff_password
):
    """Guards against the lock/constraint machinery wedging the slot shut
    forever: once the winning appointment is cancelled, the same slot must
    become bookable again (cancelled rows are excluded from both the
    row-lock pre-check and the EXCLUDE constraint's WHERE clause)."""

    def _per_request_db():
        session = TestSessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _per_request_db
    try:
        with TestClient(app) as client:
            token = _login(client, front_desk_user.email, staff_password)
            headers = {"Authorization": f"Bearer {token}"}

            start = datetime.now(timezone.utc) + timedelta(days=2)
            payload = {
                "patient_id": str(patient.id),
                "doctor_id": str(doctor_record.id),
                "room_id": str(room.id),
                "start_time": start.isoformat(),
                "duration_minutes": 20,
            }

            first = client.post("/api/v1/appointments", json=payload, headers=headers)
            assert first.status_code == 201, first.text
            appointment_id = first.json()["id"]

            blocked = client.post("/api/v1/appointments", json=payload, headers=headers)
            assert blocked.status_code == 409, blocked.text

            cancel_resp = client.delete(
                f"/api/v1/appointments/{appointment_id}",
                params={"reason": "test_cleanup"},
                headers=headers,
            )
            assert cancel_resp.status_code == 204, cancel_resp.text

            rebooked = client.post("/api/v1/appointments", json=payload, headers=headers)
            assert rebooked.status_code == 201, rebooked.text
    finally:
        app.dependency_overrides[get_db] = lambda: db
