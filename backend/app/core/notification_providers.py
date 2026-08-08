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

CONFIGURING A REAL PROVIDER (HMS Project Completion Prompt, section 3.10:
"Use safe development adapters when external credentials are unavailable.
Document how production providers can be configured."):

1. Add the provider's client library to `requirements.txt` (e.g. `twilio`
   for SMS, or plain `smtplib`/a transactional-email SDK for email; FCM's
   `firebase-admin` for push).
2. Add its credentials to `app/config.py`'s `Settings` as new fields (e.g.
   `twilio_account_sid: str | None = None`, `twilio_auth_token: str | None
   = None`, `twilio_from_number: str | None = None`), sourced from env vars
   the same way `settings.rabbitmq_url`/`settings.redis_url` already are --
   NEVER hardcode a credential in this file or anywhere else in the repo.
3. Implement the `NotificationProvider` protocol (just one method: `send(
   self, *, channel, recipient, template, payload) -> bool`) in a new class
   here, e.g. `TwilioNotificationProvider` for `channel="sms"`. Render
   `template`+`payload` into the actual message body inside `send` (this
   module has no templating engine yet -- add one, or keep it a simple
   f-string per template name, matching the project's current scale).
4. Register real instances in `_PROVIDERS`, keyed by channel, gated on
   whether credentials are configured -- e.g.:
       _PROVIDERS: dict[str, NotificationProvider] = {
           "sms": TwilioNotificationProvider(settings) if settings.twilio_account_sid else _shared_provider,
           "email": SmtpNotificationProvider(settings) if settings.smtp_host else _shared_provider,
           "push": _shared_provider,  # FCM, same pattern, when needed
       }
   This keeps every environment without real credentials (local dev, CI,
   this evaluation deployment) automatically falling back to the safe
   logging stub -- no code change needed to run without a Twilio account,
   only to run WITH one.
5. Nothing in `services/notification_engine.py` or its callers
   (`scheduling_engine.py`, `emergency_engine.py`, `lab_service.py`,
   `billing_service.py`) needs to change -- they only ever call
   `get_provider(channel).send(...)`, per the module docstring above.
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


def _redact_recipient(recipient: str) -> str:
    """Mask a phone number/email for logging: keep enough to eyeball during
    local debugging (does this look like the right recipient shape?)
    without writing the full PHI value to application logs. Whole-system
    review finding (Critical): this stub used to log `recipient` (a
    decrypted `Patient.phone`/`User.email`) and the full `payload` in
    cleartext at INFO level on every single notification send, across every
    channel -- systemic PHI-in-logs, not a one-off, since this is the only
    registered provider for sms/email/push. Never restore the un-redacted
    form here."""
    if len(recipient) <= 4:
        return "*" * len(recipient)
    return f"{'*' * (len(recipient) - 4)}{recipient[-4:]}"


class LoggingNotificationProvider:
    """Stub provider: logs that a send WOULD have happened and always
    "succeeds" (`return True`) -- it never logs the recipient or payload
    in full (see `_redact_recipient` and the module-level PHI note below).
    One instance is reused for all three channels below (`sms`/`email`/
    `push`) since the stub behavior is identical regardless of channel —
    the channel is passed through as a parameter to `send`, not baked into
    the instance. A real provider per channel (with real, channel-specific
    client setup) would instead need one instance per channel, which is
    exactly why `_PROVIDERS` is keyed by channel string rather than
    assuming a single shared instance.
    """

    def send(self, *, channel: str, recipient: str, template: str, payload: dict) -> bool:
        # PHI note: `recipient` is a decrypted phone/email and `payload` may
        # carry patient-identifying context (see notification_engine.py) --
        # neither is logged in full. Only the payload's KEYS are logged
        # (never values), enough to debug "did this event carry the fields
        # I expected" without writing PHI to disk. A real provider
        # implementation sending to an actual SMS/email/push API would not
        # have this problem (the value goes to the provider's API call, not
        # to a log line) -- this redaction exists specifically because this
        # stub's only observable side effect IS a log line.
        logger.info(
            "(stub) would send %s via %s to %s (payload keys: %s)",
            template,
            channel,
            _redact_recipient(recipient),
            sorted(payload.keys()),
        )
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
