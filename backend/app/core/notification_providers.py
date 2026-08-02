"""Pluggable per-channel notification "send" abstraction.

Same "swap-in point, not a real integration" spirit `core/events.py`'s own
docstring uses for RabbitMQ-vs-Kafka: no SMS/email/push provider
credentials exist anywhere in this scaffold's env vars, so
`LoggingNotificationProvider` logs what it would have sent and always
"succeeds". A real Twilio/SendGrid/FCM implementation is a drop-in later
(implement `NotificationProvider`, register it in `_PROVIDERS`) — not an
architecture change to `services/notification_engine.py`, which only ever
calls `get_provider(channel).send(...)`.

`recipient` is resolved by the caller (`services/notification_engine.py`,
from `Patient.phone`/`User.email`) before `send` is invoked — the provider
itself has no DB access and doesn't know how to look up a patient or user.
"""

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class NotificationProvider(Protocol):
    def send(self, *, channel: str, recipient: str, template: str, payload: dict) -> bool: ...


class UnknownNotificationChannelError(KeyError):
    """Raised by `get_provider` for a channel with no registered provider.

    Subclasses `KeyError` (the natural exception for a registry miss) but
    given its own name so callers get a clear, self-describing exception
    instead of a bare `KeyError` on an unhelpfully generic key. Shouldn't
    happen in practice given `notifications.channel`'s CHECK constraint
    (`sms`/`email`/`push` only) plus `_TOPIC_HANDLERS` only ever assigning
    one of those three — but a registry miss should raise loudly, never
    silently return `None` and let the caller crash somewhere less obvious.
    """


class LoggingNotificationProvider:
    """Stub provider: logs the send it would have made and always
    "succeeds" (`return True`). One instance is reused for all three
    channels below (`sms`/`email`/`push`) since the stub behavior is
    identical regardless of channel — the channel is passed through as a
    parameter to `send`, not baked into the instance. A real provider per
    channel (with real, channel-specific client setup) would instead need
    one instance per channel, which is exactly why `_PROVIDERS` is keyed by
    channel string rather than assuming a single shared instance.
    """

    def send(self, *, channel: str, recipient: str, template: str, payload: dict) -> bool:
        logger.info("(stub) would send %s via %s to %s: %s", template, channel, recipient, payload)
        return True


# Reusing one `LoggingNotificationProvider` instance across all three
# channels is safe: the stub is stateless (every call is independent, no
# per-channel connection/credential state to keep separate). A real
# provider swap-in would likely need distinct instances (e.g. a Twilio
# client vs. a SendGrid client vs. an FCM client), at which point this
# dict would map each channel to its own real instance instead.
_shared_provider = LoggingNotificationProvider()

_PROVIDERS: dict[str, NotificationProvider] = {
    "sms": _shared_provider,
    "email": _shared_provider,
    "push": _shared_provider,
}


def get_provider(channel: str) -> NotificationProvider:
    """Look up the provider registered for `channel`.

    Raises `UnknownNotificationChannelError` (not a silent `None`) for a
    channel with no registered provider — see that exception's docstring
    for why this shouldn't happen given the DB's own CHECK constraint, but
    is guarded against regardless.
    """
    try:
        return _PROVIDERS[channel]
    except KeyError as exc:
        raise UnknownNotificationChannelError(
            f"no notification provider registered for channel={channel!r}"
        ) from exc
