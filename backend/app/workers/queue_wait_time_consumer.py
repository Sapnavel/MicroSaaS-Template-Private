"""RabbitMQ consumer process for the Real-Time Queue & Digital Token module
(PRPs/realtime-queue-prp.md, Phase 2 / design decision #5). Mirrors
workers/notification_consumer.py's exact shape -- see that module's
docstring for why the pure-logic/broker split exists (this consumer's "pure
logic" is small enough to live inline here rather than in its own
`queue_wait_time_engine.py`). Run as its own long-lived process
(docker-compose.yml's `queue_wait_time_worker` service, `python -m
app.workers.queue_wait_time_consumer`), never imported by the FastAPI app
itself.

Binds its OWN durable queue to EXACTLY the single routing key
`queue.wait_time_updated` on the same `hms.events` topic exchange
`core/events.py`'s `RabbitMQPublisher` declares -- NOT a wildcard `#`. The
Notification Hub's own consumer originally used a wildcard and a
REVIEW-AGENT finding flagged it as contradicting that module's own PRP's
explicit "bind only the known topics" instruction; this module does not
repeat that mistake, even though (unlike Notification Hub) there is only one
topic to bind here today.

Published by `services/scheduling_engine.py`'s
`recalculate_downstream_wait_times`, payload: `{"appointment_id": "...",
"doctor_id": "..."}` (confirmed by reading that function -- no other fields
exist). On receipt: look up the `QueueToken` for that `appointment_id` (there
may be zero -- a patient may not have checked in yet -- or one; never more
than one in practice since an appointment checks in at most once). Skip
(log, ack, not an error) if no token exists, or if its status is not
`waiting`/`delayed` -- a token already `in_consultation`/`done`/`skipped` has
nothing left to re-estimate. Otherwise recompute
`estimated_wait_minutes` using the same deliberately-simple, explicitly
placeholder heuristic `queue_service`'s module docstring already documents
for this consumer (design decision #5): `(count of waiting/delayed tokens in
the same branch_id+department_id with an earlier checked_in_at) *
queue_service.AVERAGE_CONSULTATION_MINUTES`, update the row, commit, then
rebroadcast over `app.websocket.queue_board.manager` in the same message
shape `queue_service.update_status` broadcasts.

`manager.broadcast` is `async def` (design decision #4) but pika's
`basic_consume` callback is plain sync and has no surrounding event loop --
`asyncio.run(...)` is the simplest correct way to run that one broadcast
call from here, mirrored nowhere else in this file since it's the only
`await`-shaped thing this callback needs to do.

No dead-letter queue (same discipline as notification_consumer.py): a
message that raises while decoding or recomputing is logged and nacked with
`requeue=False`, i.e. dropped from the wire. The QueueToken row itself, if
already committed before a mid-broadcast failure, is unaffected -- only the
wire message / live rebroadcast is lost, and the next real event (or the
board's own GET /board snapshot) will reflect the up-to-date row regardless.
"""

import asyncio
import json
import logging

import pika
from pika.adapters.blocking_connection import BlockingChannel
from pika.spec import Basic, BasicProperties
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models.queue import QueueToken, TokenStatus
from app.services import queue_service
from app.websocket.queue_board import manager

logger = logging.getLogger(__name__)

_QUEUE_NAME = "queue_hub.wait_time_updates"
_ROUTING_KEY = "queue.wait_time_updated"

# Statuses whose estimated_wait_minutes is still meaningful to recompute --
# a token already in_consultation/done/skipped has nothing left to re-estimate
# (see module docstring).
_RECOMPUTABLE_STATUSES = (TokenStatus.waiting, TokenStatus.delayed)


def _recompute_estimated_wait_minutes(db: Session, token: QueueToken) -> int:
    """`(count of waiting/delayed tokens in the same branch_id+department_id
    with an earlier checked_in_at than `token`) *
    queue_service.AVERAGE_CONSULTATION_MINUTES` -- see module docstring's
    "Estimated-wait heuristic" note (PRP design decision #5)."""
    ahead_count = db.execute(
        select(func.count(QueueToken.id)).where(
            QueueToken.branch_id == token.branch_id,
            QueueToken.department_id == token.department_id,
            QueueToken.status.in_(_RECOMPUTABLE_STATUSES),
            QueueToken.checked_in_at < token.checked_in_at,
        )
    ).scalar_one()
    return ahead_count * queue_service.AVERAGE_CONSULTATION_MINUTES


def _handle_wait_time_updated(db: Session, payload: dict) -> None:
    appointment_id = payload["appointment_id"]
    token = db.execute(
        select(QueueToken).where(QueueToken.appointment_id == appointment_id)
    ).scalar_one_or_none()

    if token is None:
        logger.info("no queue token checked in yet for appointment_id=%s, skipping", appointment_id)
        return
    if token.status not in _RECOMPUTABLE_STATUSES:
        logger.debug(
            "queue token %s status=%s has nothing left to re-estimate, skipping",
            token.id,
            token.status.value,
        )
        return

    token.estimated_wait_minutes = _recompute_estimated_wait_minutes(db, token)
    db.add(token)
    db.commit()
    db.refresh(token)

    if token.department_id is not None:
        asyncio.run(
            manager.broadcast(
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
                },
            )
        )
    # else: department_id is None -- no topic to broadcast to, same edge
    # case queue_service.py's module docstring documents.


def _on_message(
    channel: BlockingChannel,
    method: Basic.Deliver,
    properties: BasicProperties,
    body: bytes,
) -> None:
    topic = method.routing_key
    try:
        payload = json.loads(body)
        db = SessionLocal()
        try:
            _handle_wait_time_updated(db, payload)
        finally:
            db.close()
    except Exception:
        logger.exception("failed to process queue wait-time event topic=%s", topic)
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return
    channel.basic_ack(delivery_tag=method.delivery_tag)


def run() -> None:
    connection = pika.BlockingConnection(pika.URLParameters(settings.rabbitmq_url))
    channel = connection.channel()
    channel.exchange_declare(exchange=settings.events_exchange, exchange_type="topic", durable=True)
    channel.queue_declare(queue=_QUEUE_NAME, durable=True)
    channel.queue_bind(exchange=settings.events_exchange, queue=_QUEUE_NAME, routing_key=_ROUTING_KEY)

    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(queue=_QUEUE_NAME, on_message_callback=_on_message)

    logger.info("queue wait-time consumer started, listening on queue=%s", _QUEUE_NAME)
    try:
        channel.start_consuming()
    except KeyboardInterrupt:
        channel.stop_consuming()
    finally:
        connection.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
