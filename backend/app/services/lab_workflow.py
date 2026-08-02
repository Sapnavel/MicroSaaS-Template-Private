"""Lab sample lifecycle state machine (PRPs/lab-module-prp.md, "STATE MACHINE
DESIGN"). Mirrors the rigor/shape of `services/prescription_safety.py` and
`services/patient_matching.py`: pure, DB-independent classification/validation
logic kept in its own module, separate from anything that queries Postgres or
performs writes, so the transition table and validation rules are
unit-testable with no DB fixture (this PRP's Validation Gate 1 calls that out
explicitly).

Three things worth stating plainly up front, because each is a deliberate
design decision this module's shape depends on, not an oversight:

(a) WHY STATUS LIVES ON `LabOrder`, NOT `LabSample` -- per the PRP's "Key
    design decision": schema.sql gives `lab_orders` a single `status
    lab_sample_status` column and gives `lab_samples` no status column at
    all -- only timestamp+actor pairs for the steps that have an actor
    (`collected_by`/`collected_at`, `verified_by`/`verified_at`) plus two
    actor-less timestamps (`processed_at`, `attached_to_emr_at`). The
    lifecycle position is therefore a property of the *order* (one order,
    one place in the pipeline); the *sample* row is just where the
    per-step evidence accumulates as the order moves through that
    pipeline. This module's functions all take/return `LabOrderStatus`
    accordingly -- there is no `LabSampleStatus`.

(b) WHY `result` ENTERS THE SYSTEM AT THE `verified` TRANSITION, NOT AT
    `processing` OR SOME "produced result" STEP OF ITS OWN -- the schema
    gives exactly one `result_encrypted` field and exactly two actor
    columns on `lab_samples` (`collected_by`, `verified_by`). There is no
    third actor column for "who ran the test" as distinct from "who
    verified it," and no `processed_by` column either (see `LabSample`'s
    docstring in `models/lab.py`). This module therefore conflates
    "producing the result" and "verifying the result" into the single
    `processing -> verified` transition, by necessity of the data model,
    not by clinical ideal. This is a real, documented limitation -- a
    genuine two-person "tech produces, senior verifies" handoff is NOT
    representable in this schema as it stands -- and it is intentionally
    not silently worked around here (e.g. by inventing an unpersisted
    "producer" concept). A future schema change adding a `processed_by`
    column and a distinct result-entry step would be the real fix.

(c) WHAT THIS MODULE DOES NOT DO -- it does not touch the database. It
    does not set `collected_by`, does not commit a session, does not call
    `record_audit_event`, and does not create the `LabSample` row for the
    `ordered -> collected` transition. It only answers two pure questions
    given a proposed transition: "is this transition legal from the
    current status?" and "is the accompanying `result` value legal for
    this transition?" All actual persistence, actor-stamping, and audit
    logging is Phase 2's `services/lab_service.py` responsibility -- this
    module is a validation seam that service layer calls into, exactly the
    same split `prescription_safety.py` keeps from `consultation_service.py`.
"""

from app.models.lab import LabOrderStatus

# --- Transition table (PRP "STATE MACHINE DESIGN") -------------------------
# Kept as a module-level dict, not an if/elif chain, so the PRP's diagram
# (`ordered --(collect)--> collected --(process)--> processing
# --(verify)--> verified --(attach)--> attached`) and the code can be
# diffed against each other directly -- same rationale as
# prescription_safety.py's tier tables and patient_matching.py's weights.
#
# This is a strictly linear chain -- exactly one legal "next" status from
# any given status -- not a branching graph, so a simple
# `LabOrderStatus -> LabOrderStatus` mapping (rather than
# `LabOrderStatus -> set[LabOrderStatus]`) is enough and keeps the "exactly
# one forward step" invariant visible in the type itself. `attached` has no
# entry: nothing comes after it, and looking it up with `.get()` correctly
# yields `None` for "no legal next status."
_NEXT_STATUS: dict[LabOrderStatus, LabOrderStatus] = {
    LabOrderStatus.ordered: LabOrderStatus.collected,
    LabOrderStatus.collected: LabOrderStatus.processing,
    LabOrderStatus.processing: LabOrderStatus.verified,
    LabOrderStatus.verified: LabOrderStatus.attached,
}

# The one transition where a `result` value is required: the transition
# arriving AT `verified` (i.e. `processing -> verified`, see module
# docstring point (b)). Every other transition must NOT receive one -- see
# `validate_result_for_transition`.
_TRANSITION_REQUIRING_RESULT: LabOrderStatus = LabOrderStatus.verified


def is_legal_transition(current: LabOrderStatus, requested: LabOrderStatus) -> bool:
    """Pure function, no DB access: is `current -> requested` one legal
    forward step in the lifecycle?

    Rejects (returns `False` for) every one of:
      - skipping a step (e.g. `ordered -> processing`)
      - going backward (e.g. `verified -> collected`)
      - repeating the current status (e.g. `collected -> collected`) --
        there is no re-entrant/idempotent-retry behavior in this state
        machine; a request to "transition" to the status an order is
        already in is not a no-op, it is simply not a legal transition,
        and callers must treat it the same as any other illegal request
        (a 409 state conflict, per the PRP, not a 400 or a silent success).
      - transitioning an order already in the terminal `attached` status
        (no entry in `_NEXT_STATUS` for `attached`, so `.get()` returns
        `None`, which never equals any real `requested` status).
    """
    return _NEXT_STATUS.get(current) == requested


def validate_result_for_transition(requested: LabOrderStatus, result: str | None) -> str | None:
    """Pure function, no DB access: is this `result` value legal for this
    *requested* transition (i.e. the transition arriving AT `requested`)?

    Returns `None` if the combination is legal, or a human-readable error
    message describing what's wrong (callers translate that into the
    appropriate HTTP status -- 422 for the missing-result case, per the
    PRP's "one hard data requirement" framing).

    Rules:
      - The transition landing on `verified` (i.e. `processing -> verified`)
        REQUIRES a non-empty, non-whitespace-only `result`. A missing or
        blank result here is the one hard data requirement in this whole
        state machine -- everything else is just a status flip.
      - Every other transition must NOT receive a `result` at all. A client
        attempting to sneak a `result` value in on, say, `ordered ->
        collected` or `verified -> attached` is rejected outright rather
        than having the value silently ignored -- silently dropping
        provided data is its own kind of bug (the caller would reasonably
        assume it was recorded).

    Note this takes only `requested`, not `(current, requested)`: whether a
    result is expected/forbidden depends solely on which status is being
    arrived at, not on where the order is coming from. Callers are expected
    to have already confirmed `is_legal_transition(current, requested)` is
    `True` before calling this -- this function does not re-derive that.
    """
    if requested == _TRANSITION_REQUIRING_RESULT:
        if result is None or not result.strip():
            return "A non-empty 'result' is required for the processing -> verified transition."
        return None

    if result is not None:
        return f"'result' must not be provided for a transition to '{requested.value}'."

    return None
