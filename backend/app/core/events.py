"""Thin event-publishing interface over RabbitMQ.

Kept deliberately narrow (`publish(topic, payload)`) so the scheduling and
emergency engines don't depend on RabbitMQ specifics — swapping to Kafka
later means reimplementing this one class, not touching engine code.
"""

import json
import logging
from typing import Any, Protocol

import pika

from app.config import settings

logger = logging.getLogger(__name__)


class EventPublisher(Protocol):
    def publish(self, topic: str, payload: dict[str, Any]) -> None: ...


class RabbitMQPublisher:
    def __init__(self, url: str | None = None, exchange: str | None = None) -> None:
        self._url = url or settings.rabbitmq_url
        self._exchange = exchange or settings.events_exchange

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Publish a durable event. Connection-per-publish is intentional here
        for correctness/simplicity in a scaffold; production should hold a
        pooled/long-lived channel with publisher confirms."""
        try:
            connection = pika.BlockingConnection(pika.URLParameters(self._url))
            channel = connection.channel()
            channel.exchange_declare(exchange=self._exchange, exchange_type="topic", durable=True)
            channel.basic_publish(
                exchange=self._exchange,
                routing_key=topic,
                body=json.dumps(payload, default=str).encode("utf-8"),
                properties=pika.BasicProperties(delivery_mode=2, content_type="application/json"),
            )
            connection.close()
        except Exception:
            # Publishing failure must never fail the booking transaction that
            # triggered it. Log and move on; a reconciliation job or outbox
            # pattern should back this in production.
            logger.exception("failed to publish event topic=%s", topic)


event_publisher: EventPublisher = RabbitMQPublisher()
