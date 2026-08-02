"""Redis fixed-window rate limiter for auth endpoints.

OWASP requires brute-force protection on login (PRPs/auth-rbac-abac-prp.md,
Security Design section 6). This is a small fixed-window counter — good
enough for a login endpoint's needs without pulling in a dependency like
slowapi. Uses the same `redis.Redis.from_url(settings.redis_url, ...)`
client pattern as `core/locking.py`.
"""

import logging

import redis
from fastapi import HTTPException, status

from app.config import settings

logger = logging.getLogger(__name__)

_client = redis.Redis.from_url(settings.redis_url, decode_responses=True)


def check_rate_limit(key: str, max_attempts: int, window_seconds: int) -> None:
    """Increment the fixed-window counter for `key` and raise HTTP 429 if
    `max_attempts` has been exceeded within `window_seconds`.

    The window starts on the first attempt for a given key and the counter
    self-expires via Redis TTL, so no cleanup job is needed. Callers on the
    login endpoints should key this per-email (e.g.
    f"login_attempts:{email}") and/or per-IP.
    """
    current = _client.incr(f"ratelimit:{key}")
    if current == 1:
        _client.expire(f"ratelimit:{key}", window_seconds)
    if current > max_attempts:
        logger.warning("rate limit exceeded for key=%s attempts=%s", key, current)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too many attempts, please try again later",
        )


def check_login_rate_limit(key: str) -> None:
    """Convenience wrapper using `settings.login_rate_limit_per_minute`."""
    check_rate_limit(key, max_attempts=settings.login_rate_limit_per_minute, window_seconds=60)


def check_login_ip_rate_limit(client_ip: str) -> None:
    """Per-IP counterpart to `check_login_rate_limit`. Per-email alone lets a
    credential-stuffing attacker try one password each against many distinct
    known emails from a single source without ever tripping a per-email
    counter — this closes that gap (PRPs/auth-rbac-abac-prp.md Security
    Design section 6; REVIEW-AGENT finding M1). Threshold is coarser than
    the per-email limit since one IP can legitimately host many users
    (e.g. a hospital front-desk workstation)."""
    check_rate_limit(
        f"login_ip:{client_ip}",
        max_attempts=settings.login_ip_rate_limit_per_minute,
        window_seconds=60,
    )


def check_patient_search_rate_limit(user_id: str) -> None:
    """Rate-limits GET /api/v1/patients/search per authenticated user.

    Without this, any of 4 roles could probe an unlimited number of phone
    numbers against `find a patient with this phone` with no throttle at
    all -- PHI-adjacent enumeration (REVIEW-AGENT finding M1,
    patient-master-index-prp.md Phase 3). Reuses this module's existing
    fixed-window primitive rather than adding new infrastructure.
    """
    check_rate_limit(
        f"patient_search:{user_id}",
        max_attempts=settings.patient_search_rate_limit_per_minute,
        window_seconds=60,
    )
