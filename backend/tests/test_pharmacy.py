"""Integration tests for the Pharmacy Inventory module (routers/pharmacy.py,
services/pharmacy_service.py, services/pharmacy_engine.py), per
PRPs/pharmacy-module-prp.md Phase 3. Runs against a real Postgres + Redis
(see tests/conftest.py's module docstring), reusing fixtures from prior
modules' TEST-AGENTs (`db`, `client`, `branch`, `other_branch`,
`system_admin_user`) plus this module's own `pharmacist_user`,
`other_branch_pharmacist_user`, `drug`, `other_drug`.

Branch tenancy is REAL in this module (see models/pharmacy.py's module
docstring) -- unlike Patient/Consultation/Lab, which are all deliberately
branch-agnostic. Tests below treat a cross-branch stock leak with the same
severity prior modules' tests gave a PHI leak.

Most setup below inserts `InventoryItem`/`InventoryBatch` rows directly via
the ORM (helpers `_make_item`/`_make_batch`) rather than through the API --
faster and gives exact control over `expiry_date`/`quantity` for the FEFO
scenarios, which is the "direct calls ... where more direct than going
through HTTP" case the task brief calls out. The dispense/receive/items
endpoints themselves are still exercised through `TestClient` for every
test that is actually about those endpoints' behavior (status codes,
response shape, branch resolution).
"""

import threading
import time
import uuid
from datetime import date, timedelta

from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.pharmacy import InventoryBatch, InventoryItem
from app.services import pharmacy_engine
from app.services.pharmacy_engine import InsufficientStockError
from tests.conftest import TestSessionLocal

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _login(client, email: str, password: str) -> str:
    resp = client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_item(db, branch_id, drug_id, reorder_threshold: int = 0) -> InventoryItem:
    item = InventoryItem(branch_id=branch_id, drug_id=drug_id, reorder_threshold=reorder_threshold)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _make_batch(db, inventory_item_id, quantity: int, expiry_date, batch_number: str | None = None) -> InventoryBatch:
    batch = InventoryBatch(
        inventory_item_id=inventory_item_id,
        batch_number=batch_number or f"BATCH-{uuid.uuid4().hex[:8]}",
        quantity=quantity,
        expiry_date=expiry_date,
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


def _refresh_qty(db, batch_id) -> int:
    """Re-query a batch's current quantity from the DB (not from a
    possibly-stale in-memory ORM object) -- used to confirm dispense's
    all-or-nothing guarantee across the tests below."""
    return db.execute(select(InventoryBatch.quantity).where(InventoryBatch.id == batch_id)).scalar_one()


TODAY = date.today()

# ---------------------------------------------------------------------------
# POST /api/v1/pharmacy/items
# ---------------------------------------------------------------------------


def test_create_item_201_on_first_creation(client, pharmacist_user, staff_password, drug):
    token = _login(client, pharmacist_user.email, staff_password)

    resp = client.post(
        "/api/v1/pharmacy/items",
        json={"drug_id": str(drug.id), "reorder_threshold": 10},
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["drug_id"] == str(drug.id)
    assert body["branch_id"] == str(pharmacist_user.branch_id)
    assert body["reorder_threshold"] == 10


def test_create_item_200_on_second_call_updates_not_duplicates(client, db, pharmacist_user, staff_password, drug):
    token = _login(client, pharmacist_user.email, staff_password)

    first = client.post(
        "/api/v1/pharmacy/items",
        json={"drug_id": str(drug.id), "reorder_threshold": 10},
        headers=_auth(token),
    )
    assert first.status_code == 201, first.text

    second = client.post(
        "/api/v1/pharmacy/items",
        json={"drug_id": str(drug.id), "reorder_threshold": 25},
        headers=_auth(token),
    )
    assert second.status_code == 200, second.text
    assert second.json()["reorder_threshold"] == 25
    assert second.json()["id"] == first.json()["id"]

    count = db.execute(
        select(InventoryItem).where(
            InventoryItem.branch_id == pharmacist_user.branch_id,
            InventoryItem.drug_id == drug.id,
        )
    ).scalars().all()
    assert len(count) == 1
    assert count[0].reorder_threshold == 25


def test_create_item_422_pharmacist_branch_id_mismatch(client, pharmacist_user, staff_password, drug, other_branch):
    token = _login(client, pharmacist_user.email, staff_password)

    resp = client.post(
        "/api/v1/pharmacy/items",
        json={"drug_id": str(drug.id), "reorder_threshold": 10, "branch_id": str(other_branch.id)},
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text


def test_create_item_422_system_admin_omits_branch_id(client, system_admin_user, staff_password, drug):
    token = _login(client, system_admin_user.email, staff_password)

    resp = client.post(
        "/api/v1/pharmacy/items",
        json={"drug_id": str(drug.id), "reorder_threshold": 10},
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text


def test_create_item_system_admin_supplies_branch_id_succeeds(client, system_admin_user, staff_password, drug, branch):
    token = _login(client, system_admin_user.email, staff_password)

    resp = client.post(
        "/api/v1/pharmacy/items",
        json={"drug_id": str(drug.id), "reorder_threshold": 10, "branch_id": str(branch.id)},
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["branch_id"] == str(branch.id)


# ---------------------------------------------------------------------------
# POST /api/v1/pharmacy/batches
# ---------------------------------------------------------------------------


def test_receive_batch_201(client, pharmacist_user, staff_password, drug):
    token = _login(client, pharmacist_user.email, staff_password)

    resp = client.post(
        "/api/v1/pharmacy/batches",
        json={
            "drug_id": str(drug.id),
            "batch_number": "LOT-001",
            "quantity": 50,
            "expiry_date": (TODAY + timedelta(days=90)).isoformat(),
        },
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["batch_number"] == "LOT-001"
    assert body["quantity"] == 50


def test_receive_batch_auto_creates_inventory_item_with_zero_threshold(
    client, db, pharmacist_user, staff_password, drug
):
    """First-time stock receipt for a drug this branch has never stocked --
    per the PRP, auto-creates the `InventoryItem` with `reorder_threshold=0`
    rather than 404ing."""
    token = _login(client, pharmacist_user.email, staff_password)

    pre_existing = db.execute(
        select(InventoryItem).where(
            InventoryItem.branch_id == pharmacist_user.branch_id,
            InventoryItem.drug_id == drug.id,
        )
    ).scalar_one_or_none()
    assert pre_existing is None

    resp = client.post(
        "/api/v1/pharmacy/batches",
        json={
            "drug_id": str(drug.id),
            "batch_number": "LOT-FIRST",
            "quantity": 20,
            "expiry_date": (TODAY + timedelta(days=60)).isoformat(),
        },
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text

    item = db.execute(
        select(InventoryItem).where(
            InventoryItem.branch_id == pharmacist_user.branch_id,
            InventoryItem.drug_id == drug.id,
        )
    ).scalar_one()
    assert item.reorder_threshold == 0


def test_receive_batch_writes_audit_log(client, db, pharmacist_user, staff_password, drug):
    token = _login(client, pharmacist_user.email, staff_password)

    resp = client.post(
        "/api/v1/pharmacy/batches",
        json={
            "drug_id": str(drug.id),
            "batch_number": "LOT-AUDIT",
            "quantity": 15,
            "expiry_date": (TODAY + timedelta(days=45)).isoformat(),
        },
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    batch_id = resp.json()["id"]

    audit_row = db.execute(
        select(AuditLog).where(
            AuditLog.action == "pharmacy.batch_received",
            AuditLog.resource_id == str(batch_id),
        )
    ).scalar_one()
    assert audit_row.event_metadata["batch_number"] == "LOT-AUDIT"
    assert audit_row.event_metadata["quantity"] == 15
    assert audit_row.event_metadata["drug_id"] == str(drug.id)


# ---------------------------------------------------------------------------
# POST /api/v1/pharmacy/dispense
# ---------------------------------------------------------------------------


def test_dispense_pulls_fefo_across_two_batches_partial_on_second_third_untouched(
    client, db, pharmacist_user, staff_password, drug
):
    item = _make_item(db, pharmacist_user.branch_id, drug.id, reorder_threshold=0)
    b1 = _make_batch(db, item.id, quantity=5, expiry_date=TODAY + timedelta(days=10))
    b2 = _make_batch(db, item.id, quantity=20, expiry_date=TODAY + timedelta(days=20))
    b3 = _make_batch(db, item.id, quantity=100, expiry_date=TODAY + timedelta(days=30))

    token = _login(client, pharmacist_user.email, staff_password)
    resp = client.post(
        "/api/v1/pharmacy/dispense",
        json={"drug_id": str(drug.id), "quantity": 12, "reference": "rx-123"},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["quantity_dispensed"] == 12
    allocations = body["allocations"]
    assert len(allocations) == 2
    assert allocations[0]["batch_id"] == str(b1.id)
    assert allocations[0]["quantity_taken"] == 5
    assert allocations[1]["batch_id"] == str(b2.id)
    assert allocations[1]["quantity_taken"] == 7

    assert _refresh_qty(db, b1.id) == 0
    assert _refresh_qty(db, b2.id) == 13
    assert _refresh_qty(db, b3.id) == 100  # latest-expiry batch untouched


def test_dispense_409_insufficient_stock_nothing_decremented(client, db, pharmacist_user, staff_password, drug):
    item = _make_item(db, pharmacist_user.branch_id, drug.id, reorder_threshold=0)
    b1 = _make_batch(db, item.id, quantity=4, expiry_date=TODAY + timedelta(days=10))
    b2 = _make_batch(db, item.id, quantity=6, expiry_date=TODAY + timedelta(days=20))

    token = _login(client, pharmacist_user.email, staff_password)
    resp = client.post(
        "/api/v1/pharmacy/dispense",
        json={"drug_id": str(drug.id), "quantity": 999},
        headers=_auth(token),
    )

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["available"] == 10
    assert body["requested"] == 999

    assert _refresh_qty(db, b1.id) == 4
    assert _refresh_qty(db, b2.id) == 6


def test_dispense_excludes_expired_batch_even_as_only_candidate(client, db, pharmacist_user, staff_password, drug):
    """An expired batch is invisible to FEFO allocation entirely -- even
    when it is the ONLY batch with stock and the request would otherwise
    fail cleanly, `available` in the 409 body must exclude its quantity
    rather than count it."""
    item = _make_item(db, pharmacist_user.branch_id, drug.id, reorder_threshold=0)
    expired = _make_batch(db, item.id, quantity=50, expiry_date=TODAY - timedelta(days=1))

    token = _login(client, pharmacist_user.email, staff_password)
    resp = client.post(
        "/api/v1/pharmacy/dispense",
        json={"drug_id": str(drug.id), "quantity": 10},
        headers=_auth(token),
    )

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["available"] == 0  # expired batch's 50 units correctly excluded
    assert body["requested"] == 10

    assert _refresh_qty(db, expired.id) == 50  # untouched


def test_dispense_expired_batch_never_drawn_when_valid_stock_also_exists(
    client, db, pharmacist_user, staff_password, drug
):
    """Even when a non-expired batch could (incorrectly) be skipped in favor
    of an earlier-expiring-but-already-expired batch, the expired one must
    never be selected -- the valid batch is drawn from instead."""
    item = _make_item(db, pharmacist_user.branch_id, drug.id, reorder_threshold=0)
    expired = _make_batch(db, item.id, quantity=999, expiry_date=TODAY - timedelta(days=5))
    valid = _make_batch(db, item.id, quantity=8, expiry_date=TODAY + timedelta(days=5))

    token = _login(client, pharmacist_user.email, staff_password)
    resp = client.post(
        "/api/v1/pharmacy/dispense",
        json={"drug_id": str(drug.id), "quantity": 8},
        headers=_auth(token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["allocations"]) == 1
    assert body["allocations"][0]["batch_id"] == str(valid.id)

    assert _refresh_qty(db, expired.id) == 999  # never touched
    assert _refresh_qty(db, valid.id) == 0


def test_dispense_404_unknown_drug_branch_combination(client, pharmacist_user, staff_password, other_drug):
    """No `InventoryItem` has ever been created for `(branch, other_drug)`."""
    token = _login(client, pharmacist_user.email, staff_password)

    resp = client.post(
        "/api/v1/pharmacy/dispense",
        json={"drug_id": str(other_drug.id), "quantity": 1},
        headers=_auth(token),
    )

    assert resp.status_code == 404, resp.text


def test_dispense_writes_sane_audit_log(client, db, pharmacist_user, staff_password, drug):
    item = _make_item(db, pharmacist_user.branch_id, drug.id, reorder_threshold=0)
    batch = _make_batch(db, item.id, quantity=10, expiry_date=TODAY + timedelta(days=10))

    token = _login(client, pharmacist_user.email, staff_password)
    resp = client.post(
        "/api/v1/pharmacy/dispense",
        json={"drug_id": str(drug.id), "quantity": 4, "reference": "rx-456"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text

    audit_row = db.execute(
        select(AuditLog).where(
            AuditLog.action == "pharmacy.dispensed",
            AuditLog.resource_id == str(item.id),
        )
    ).scalar_one()
    metadata = audit_row.event_metadata

    # Sane shape: exactly the expected top-level keys, nothing extra (this
    # module carries no PHI, so this check is deliberately lighter-weight
    # than prior modules' PHI-leak audits -- just confirming the shape).
    assert set(metadata.keys()) == {"drug_id", "branch_id", "quantity", "reference", "allocations"}
    assert metadata["drug_id"] == str(drug.id)
    assert metadata["branch_id"] == str(pharmacist_user.branch_id)
    assert metadata["quantity"] == 4
    assert metadata["reference"] == "rx-456"
    assert len(metadata["allocations"]) == 1
    allocation = metadata["allocations"][0]
    assert set(allocation.keys()) == {"batch_id", "batch_number", "expiry_date", "quantity_taken"}
    assert allocation["batch_id"] == str(batch.id)
    assert allocation["quantity_taken"] == 4


def test_dispense_query_uses_select_for_update(drug):
    """Defense-in-depth confirmation (in addition to the multi-connection
    test below) that the FEFO candidate query is actually built with
    `.with_for_update()` -- read straight from the compiled SQL rather than
    just trusting the source comment."""
    import inspect

    source = inspect.getsource(pharmacy_engine.dispense)
    assert "with_for_update()" in source


def test_dispense_concurrent_requests_do_not_oversell(pharmacist_user, drug):
    """Genuine two-connection concurrency test: two separate DB sessions
    (separate Postgres connections, via `TestSessionLocal` -- same class
    conftest.py's `db` fixture uses) each call `pharmacy_engine.dispense`
    directly against the SAME batch, on separate threads. Thread A locks the
    batch's row via `SELECT ... FOR UPDATE`, sleeps briefly before
    committing (to give thread B a wide window to start and attempt its own
    lock acquisition), and thread B's `dispense` call must BLOCK on that
    lock until A commits -- then re-read the now-decremented quantity,
    rather than racing against a stale value. This is the exact TOCTOU shape
    `services/lab_service.py`'s original bug had; this module was built with
    the fix from the start, and this test exercises the real Postgres lock,
    not just a source-code check.

    Total stock: 10. Thread A requests 7 (succeeds, leaving 3). Thread B
    requests 5, started ~0.3s after A (well after A's lock is acquired, well
    before A's artificial 1s pre-commit sleep elapses) -- if B raced against
    the STALE pre-A quantity (10), it would wrongly believe 5 is available
    and either oversell or corrupt the row. Because B's SELECT blocks until
    A's commit, B correctly sees quantity=3 post-A and gets a clean
    `InsufficientStockError(available=3, requested=5)` -- total actually
    dispensed across both threads is 7, never exceeding the true available
    stock of 10.
    """
    setup_db = TestSessionLocal()
    try:
        item = InventoryItem(branch_id=pharmacist_user.branch_id, drug_id=drug.id, reorder_threshold=0)
        setup_db.add(item)
        setup_db.commit()
        setup_db.refresh(item)
        batch = InventoryBatch(
            inventory_item_id=item.id,
            batch_number="LOT-CONCURRENCY",
            quantity=10,
            expiry_date=TODAY + timedelta(days=10),
        )
        setup_db.add(batch)
        setup_db.commit()
        setup_db.refresh(batch)
        item_id, batch_id, drug_id, branch_id = item.id, batch.id, drug.id, pharmacist_user.branch_id
    finally:
        setup_db.close()

    results: dict[str, object] = {}

    def _thread_a() -> None:
        db_a = TestSessionLocal()
        try:
            result = pharmacy_engine.dispense(
                db_a,
                branch_id=branch_id,
                drug_id=drug_id,
                quantity=7,
                actor_user_id=pharmacist_user.id,
            )
            results["a_quantity"] = result.quantity_dispensed
            time.sleep(1.0)  # hold the row lock open
            db_a.commit()
        except Exception as exc:  # noqa: BLE001 - surfaced to the main thread via `results`
            results["a_error"] = exc
            db_a.rollback()
        finally:
            db_a.close()

    def _thread_b() -> None:
        time.sleep(0.3)  # ensure thread A has already acquired the row lock
        db_b = TestSessionLocal()
        try:
            result = pharmacy_engine.dispense(
                db_b,
                branch_id=branch_id,
                drug_id=drug_id,
                quantity=5,
                actor_user_id=pharmacist_user.id,
            )
            results["b_quantity"] = result.quantity_dispensed
            db_b.commit()
        except InsufficientStockError as exc:
            results["b_error"] = exc
            db_b.rollback()
        finally:
            db_b.close()

    t_a = threading.Thread(target=_thread_a)
    t_b = threading.Thread(target=_thread_b)
    t_a.start()
    t_b.start()
    t_a.join(timeout=10)
    t_b.join(timeout=10)

    assert "a_error" not in results, results.get("a_error")
    assert results.get("a_quantity") == 7

    assert "b_quantity" not in results, "thread B must not have succeeded -- that would be overselling"
    b_error = results.get("b_error")
    assert isinstance(b_error, InsufficientStockError)
    assert b_error.available == 3  # post-A quantity, proving B blocked and re-read, not raced on stale data
    assert b_error.requested == 5

    verify_db = TestSessionLocal()
    try:
        final_qty = _refresh_qty(verify_db, batch_id)
    finally:
        verify_db.close()
    assert final_qty == 3  # 10 - 7 (A only); total dispensed (7) never exceeded available stock (10)


# ---------------------------------------------------------------------------
# GET /api/v1/pharmacy/low-stock
# ---------------------------------------------------------------------------


def test_low_stock_below_threshold_appears_above_does_not(client, db, pharmacist_user, staff_password, drug, other_drug):
    low_item = _make_item(db, pharmacist_user.branch_id, drug.id, reorder_threshold=20)
    _make_batch(db, low_item.id, quantity=5, expiry_date=TODAY + timedelta(days=10))

    ok_item = _make_item(db, pharmacist_user.branch_id, other_drug.id, reorder_threshold=5)
    _make_batch(db, ok_item.id, quantity=50, expiry_date=TODAY + timedelta(days=10))

    token = _login(client, pharmacist_user.email, staff_password)
    resp = client.get("/api/v1/pharmacy/low-stock", headers=_auth(token))

    assert resp.status_code == 200, resp.text
    items_by_drug = {row["drug_id"]: row for row in resp.json()}
    assert str(drug.id) in items_by_drug
    assert items_by_drug[str(drug.id)]["current_quantity"] == 5
    assert str(other_drug.id) not in items_by_drug


def test_low_stock_expired_batches_excluded_from_current_quantity(client, db, pharmacist_user, staff_password, drug):
    """`current_quantity` sums only non-expired batches -- an expired batch
    with a large quantity must not mask a genuinely low-stock item."""
    item = _make_item(db, pharmacist_user.branch_id, drug.id, reorder_threshold=20)
    _make_batch(db, item.id, quantity=5, expiry_date=TODAY + timedelta(days=10))
    _make_batch(db, item.id, quantity=999, expiry_date=TODAY - timedelta(days=1))  # expired, must not count

    token = _login(client, pharmacist_user.email, staff_password)
    resp = client.get("/api/v1/pharmacy/low-stock", headers=_auth(token))

    assert resp.status_code == 200, resp.text
    items_by_drug = {row["drug_id"]: row for row in resp.json()}
    assert items_by_drug[str(drug.id)]["current_quantity"] == 5


def test_low_stock_pharmacist_sees_only_own_branch(
    client, db, pharmacist_user, staff_password, other_branch_pharmacist_user, drug, other_drug
):
    own_item = _make_item(db, pharmacist_user.branch_id, drug.id, reorder_threshold=20)
    _make_batch(db, own_item.id, quantity=1, expiry_date=TODAY + timedelta(days=10))

    other_item = _make_item(db, other_branch_pharmacist_user.branch_id, other_drug.id, reorder_threshold=20)
    _make_batch(db, other_item.id, quantity=1, expiry_date=TODAY + timedelta(days=10))

    token = _login(client, pharmacist_user.email, staff_password)
    resp = client.get("/api/v1/pharmacy/low-stock", headers=_auth(token))

    assert resp.status_code == 200, resp.text
    drug_ids = {row["drug_id"] for row in resp.json()}
    assert str(drug.id) in drug_ids
    assert str(other_drug.id) not in drug_ids


def test_low_stock_system_admin_omits_branch_id_sees_both_branches(
    client, db, system_admin_user, staff_password, pharmacist_user, other_branch_pharmacist_user, drug, other_drug
):
    own_item = _make_item(db, pharmacist_user.branch_id, drug.id, reorder_threshold=20)
    _make_batch(db, own_item.id, quantity=1, expiry_date=TODAY + timedelta(days=10))

    other_item = _make_item(db, other_branch_pharmacist_user.branch_id, other_drug.id, reorder_threshold=20)
    _make_batch(db, other_item.id, quantity=1, expiry_date=TODAY + timedelta(days=10))

    token = _login(client, system_admin_user.email, staff_password)
    resp = client.get("/api/v1/pharmacy/low-stock", headers=_auth(token))

    assert resp.status_code == 200, resp.text
    drug_ids = {row["drug_id"] for row in resp.json()}
    assert str(drug.id) in drug_ids
    assert str(other_drug.id) in drug_ids


# ---------------------------------------------------------------------------
# GET /api/v1/pharmacy/expiring
# ---------------------------------------------------------------------------


def test_expiring_within_window_appears_beyond_window_does_not(
    client, db, pharmacist_user, staff_password, drug, other_drug
):
    within_item = _make_item(db, pharmacist_user.branch_id, drug.id, reorder_threshold=0)
    within_batch = _make_batch(db, within_item.id, quantity=10, expiry_date=TODAY + timedelta(days=10))

    beyond_item = _make_item(db, pharmacist_user.branch_id, other_drug.id, reorder_threshold=0)
    _make_batch(db, beyond_item.id, quantity=10, expiry_date=TODAY + timedelta(days=90))

    token = _login(client, pharmacist_user.email, staff_password)
    resp = client.get("/api/v1/pharmacy/expiring", params={"within_days": 30}, headers=_auth(token))

    assert resp.status_code == 200, resp.text
    batch_ids = {row["batch_id"] for row in resp.json()}
    assert str(within_batch.id) in batch_ids
    assert len(resp.json()) == 1  # the beyond-window batch must not appear


def test_expiring_already_expired_batch_never_appears(client, db, pharmacist_user, staff_password, drug):
    item = _make_item(db, pharmacist_user.branch_id, drug.id, reorder_threshold=0)
    expired_batch = _make_batch(db, item.id, quantity=10, expiry_date=TODAY - timedelta(days=1))

    token = _login(client, pharmacist_user.email, staff_password)
    # Even a huge within_days window must never surface an already-expired batch.
    resp = client.get("/api/v1/pharmacy/expiring", params={"within_days": 3650}, headers=_auth(token))

    assert resp.status_code == 200, resp.text
    batch_ids = {row["batch_id"] for row in resp.json()}
    assert str(expired_batch.id) not in batch_ids


def test_expiring_pharmacist_sees_only_own_branch(
    client, db, pharmacist_user, staff_password, other_branch_pharmacist_user, drug, other_drug
):
    own_item = _make_item(db, pharmacist_user.branch_id, drug.id, reorder_threshold=0)
    own_batch = _make_batch(db, own_item.id, quantity=10, expiry_date=TODAY + timedelta(days=10))

    other_item = _make_item(db, other_branch_pharmacist_user.branch_id, other_drug.id, reorder_threshold=0)
    other_batch = _make_batch(db, other_item.id, quantity=10, expiry_date=TODAY + timedelta(days=10))

    token = _login(client, pharmacist_user.email, staff_password)
    resp = client.get("/api/v1/pharmacy/expiring", params={"within_days": 30}, headers=_auth(token))

    assert resp.status_code == 200, resp.text
    batch_ids = {row["batch_id"] for row in resp.json()}
    assert str(own_batch.id) in batch_ids
    assert str(other_batch.id) not in batch_ids


def test_expiring_system_admin_omits_branch_id_sees_both_branches(
    client, db, system_admin_user, staff_password, pharmacist_user, other_branch_pharmacist_user, drug, other_drug
):
    own_item = _make_item(db, pharmacist_user.branch_id, drug.id, reorder_threshold=0)
    own_batch = _make_batch(db, own_item.id, quantity=10, expiry_date=TODAY + timedelta(days=10))

    other_item = _make_item(db, other_branch_pharmacist_user.branch_id, other_drug.id, reorder_threshold=0)
    other_batch = _make_batch(db, other_item.id, quantity=10, expiry_date=TODAY + timedelta(days=10))

    token = _login(client, system_admin_user.email, staff_password)
    resp = client.get("/api/v1/pharmacy/expiring", params={"within_days": 30}, headers=_auth(token))

    assert resp.status_code == 200, resp.text
    batch_ids = {row["batch_id"] for row in resp.json()}
    assert str(own_batch.id) in batch_ids
    assert str(other_batch.id) in batch_ids


# ---------------------------------------------------------------------------
# Branch isolation
# ---------------------------------------------------------------------------


def test_dispense_422_pharmacist_mismatched_branch_id_in_body(
    client, db, pharmacist_user, staff_password, other_branch, drug
):
    item = _make_item(db, pharmacist_user.branch_id, drug.id, reorder_threshold=0)
    _make_batch(db, item.id, quantity=10, expiry_date=TODAY + timedelta(days=10))

    token = _login(client, pharmacist_user.email, staff_password)
    resp = client.post(
        "/api/v1/pharmacy/dispense",
        json={"drug_id": str(drug.id), "quantity": 1, "branch_id": str(other_branch.id)},
        headers=_auth(token),
    )

    assert resp.status_code == 422, resp.text


def test_dispense_404_when_drug_only_stocked_at_other_branch_no_branch_id_given(
    client, db, pharmacist_user, staff_password, other_branch_pharmacist_user, drug
):
    """The critical branch-isolation case: a pharmacist at `branch` dispenses
    against `drug_id` that only has stock at `other_branch`, WITHOUT passing
    any `branch_id` at all. `_resolve_branch_id` always resolves a
    pharmacist to their OWN branch_id regardless of what the drug_id happens
    to be stocked at elsewhere -- this must 404 as "not stocked at YOUR
    branch", never silently draw from `other_branch`'s batches."""
    other_item = _make_item(db, other_branch_pharmacist_user.branch_id, drug.id, reorder_threshold=0)
    other_batch = _make_batch(db, other_item.id, quantity=100, expiry_date=TODAY + timedelta(days=10))

    token = _login(client, pharmacist_user.email, staff_password)
    resp = client.post(
        "/api/v1/pharmacy/dispense",
        json={"drug_id": str(drug.id), "quantity": 5},
        headers=_auth(token),
    )

    assert resp.status_code == 404, resp.text
    # other_branch's stock must be completely untouched.
    assert _refresh_qty(db, other_batch.id) == 100
