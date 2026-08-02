"""Distributed locking for the tri-resource booking engine.

Implements the Redlock algorithm's single-instance primitive (SET NX PX +
token-checked release via Lua). In production, point `redis_url` at N=3 (or
5) independent Redis masters and acquire/release against a majority per the
Redlock spec (https://redis.io/docs/manual/patterns/distributed-locks/) —
this class is written so that swapping `_client` for a
`RedlockMultiInstance` wrapper (same interface) is the only change needed.

This lock is the *fast-fail* layer: it prevents two API processes from even
starting conflicting transactions. It is NOT the source of correctness —
the Postgres EXCLUDE constraints in database/schema.sql are. See
docs/ARCHITECTURE.md section 5.
"""

import time
import uuid
from contextlib import contextmanager
from types import TracebackType

import redis

from app.config import settings

_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


class LockAcquisitionError(Exception):
    """Raised when a resource lock could not be acquired within the retry budget."""


class ResourceLock:
    """A single named distributed lock, held via context manager."""

    def __init__(self, client: redis.Redis, key: str, ttl_ms: int) -> None:
        self._client = client
        self.key = f"lock:{key}"
        self._ttl_ms = ttl_ms
        self._token = str(uuid.uuid4())
        self._release_script = client.register_script(_RELEASE_SCRIPT)

    def acquire(self, retries: int = 10, retry_delay_s: float = 0.05) -> bool:
        for _ in range(retries):
            if self._client.set(self.key, self._token, nx=True, px=self._ttl_ms):
                return True
            time.sleep(retry_delay_s)
        return False

    def release(self) -> None:
        self._release_script(keys=[self.key], args=[self._token])

    def __enter__(self) -> "ResourceLock":
        if not self.acquire():
            raise LockAcquisitionError(f"could not acquire lock for {self.key}")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


class LockManager:
    """Acquires multiple resource locks atomically-enough for our purposes.

    Locks are always acquired in sorted key order across the whole process,
    so two callers requesting overlapping resource sets can never deadlock
    each other (classic lock-ordering discipline).
    """

    def __init__(self, redis_url: str | None = None, ttl_ms: int | None = None) -> None:
        self._client = redis.Redis.from_url(redis_url or settings.redis_url, decode_responses=True)
        self._ttl_ms = ttl_ms or settings.redis_lock_ttl_ms

    @contextmanager
    def acquire_all(self, resource_keys: list[str]):
        """Acquire locks for every key in `resource_keys`, in sorted order.

        Raises LockAcquisitionError if any lock can't be obtained; already
        acquired locks are released before the exception propagates.
        """
        ordered_keys = sorted(set(resource_keys))
        locks: list[ResourceLock] = []
        try:
            for key in ordered_keys:
                lock = ResourceLock(self._client, key, self._ttl_ms)
                if not lock.acquire():
                    raise LockAcquisitionError(f"resource busy: {key}")
                locks.append(lock)
            yield
        finally:
            for lock in reversed(locks):
                lock.release()


lock_manager = LockManager()
