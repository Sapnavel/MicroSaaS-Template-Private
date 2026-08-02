"""RabbitMQ consumer process for the Notification & Alert Hub module
(PRPs/notification-hub-prp.md, Phase 2). Thin wrapper around
services/notification_engine.handle_event -- see that module's docstring
for why the pure-logic/broker split exists. Run as its own long-lived
process (docker-compose.yml's `notification_worker` service, `python -m
app.workers.notification_consumer`), never imported by the FastAPI app
itself.

Binds a single durable queue to exactly the routing keys in
`notification_engine._TOPIC_HANDLERS` on the same `hms.events` topic
exchange `core/events.py`'s `RabbitMQPublisher` declares -- NOT a wildcard
`#` (per the PRP's explicit instruction): an unrecognized topic should stay
a deliberate no-op inside `handle_event`, never something this queue
silently starts consuming just because it happens to get published,
possibly meant for a different consumer entirely. A future fifth topic is
still a one-entry-registry addition: add its handler to `_TOPIC_HANDLERS`
and it is bound here automatically, no change to this file needed.

No dead-letter queue exists (PRP design decision #2): a message that raises
while decoding or in `handle_event` is logged and nacked with
`requeue=False`, i.e. dropped from the wire. Any `Notification` row already
committed as `status="queued"` before a mid-send failure is unaffected --
only the wire message is lost, and that row can still be retried manually
via `PATCH /notifications/{id}/retry`.
"""

import json
import logging

import pika
from pika.adapters.blocking_connection import BlockingChannel
from pika.spec import Basic, BasicProperties

from app.config import settings
from app.database import SessionLocal
from app.services.notification_engine import _TOPIC_HANDLERS, handle_event

logger = logging.getLogger(__name__)

_QUEUE_NAME = "notification_hub.events"


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
            handle_event(db, topic=topic, payload=payload)
        finally:
            db.close()
    except Exception:
        logger.exception("failed to process notification event topic=%s", topic)
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return
    channel.basic_ack(delivery_tag=method.delivery_tag)


def run() -> None:
    connection = pika.BlockingConnection(pika.URLParameters(settings.rabbitmq_url))
    channel = connection.channel()
    channel.exchange_declare(exchange=settings.events_exchange, exchange_type="topic", durable=True)
    channel.queue_declare(queue=_QUEUE_NAME, durable=True)
    for topic in _TOPIC_HANDLERS:
        channel.queue_bind(exchange=settings.events_exchange, queue=_QUEUE_NAME, routing_key=topic)

    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(queue=_QUEUE_NAME, on_message_callback=_on_message)

    logger.info("notification consumer started, listening on queue=%s", _QUEUE_NAME)
    try:
        channel.start_consuming()
    except KeyboardInterrupt:
        channel.stop_consuming()
    finally:
        connection.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
