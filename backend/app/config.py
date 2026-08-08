import json
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql+psycopg2://user:password@localhost:5432/hms"

    # Auth
    # JWT signing keyring: {"kid": "secret"}. `jwt_current_kid` selects which
    # entry signs *new* tokens; every entry is still tried on verification so
    # tokens signed with a previous kid keep working during key rotation
    # (see PRPs/auth-rbac-abac-prp.md, Security Design section 1).
    jwt_signing_keys: dict[str, str] = {"k1": "change-me-in-production"}
    jwt_current_kid: str = "k1"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7

    # HMAC key for deterministic PHI-matching hashes (core/encryption.py).
    # Deliberately separate from the JWT signing keyring so rotating JWT
    # keys never changes the hash of an already-stored phone/national-id.
    hmac_key: str = "change-me-in-production"

    # Login rate limiting (core/rate_limit.py). Per-email AND per-IP, per
    # PRPs/auth-rbac-abac-prp.md Security Design section 6 — per-email alone
    # doesn't stop credential stuffing across many distinct known emails
    # from one source (REVIEW-AGENT finding M1).
    login_rate_limit_per_minute: int = 5
    login_ip_rate_limit_per_minute: int = 20

    # Patient search rate limiting (core/rate_limit.py). GET /api/v1/patients/search
    # lets staff confirm whether a phone number belongs to an existing
    # patient -- PHI-adjacent enumeration, same class of risk as the login
    # rate limits above (REVIEW-AGENT finding M1, patient-master-index-prp.md
    # Phase 3). Keyed per authenticated user, not per-IP, since callers are
    # already-authenticated staff and a shared front-desk workstation IP
    # shouldn't throttle multiple legitimate users; higher than the login
    # limit since normal registration workflow involves several searches.
    patient_search_rate_limit_per_minute: int = 30

    # HMS Project Completion Prompt gap ("rate limiting where appropriate"):
    # a broad, generous per-user (falls back to per-IP for unauthenticated
    # requests) limit on every mutating request (POST/PUT/PATCH/DELETE)
    # across the whole API (core/rate_limit.py's `check_write_rate_limit`,
    # wired in main.py's `_write_rate_limit` middleware) -- defense in depth
    # against bulk/scripted abuse of write-heavy endpoints (registration,
    # merge, billing writes, etc.) that had no throttle at all before this.
    # Deliberately much higher than the login/patient-search limits above,
    # which stay in place as the tighter, endpoint-specific guards for their
    # specific enumeration/brute-force risks -- this one is a coarse
    # backstop, not meant to interfere with normal UI usage.
    write_rate_limit_per_minute: int = 120

    google_client_id: str | None = None
    google_client_secret: str | None = None

    # PHI field-level encryption key (32-byte urlsafe base64, Fernet-compatible)
    phi_encryption_key: str = "change-me-32-byte-urlsafe-base64-fernet-key="

    # Redis (distributed locking)
    redis_url: str = "redis://localhost:6379/0"
    redis_lock_ttl_ms: int = 5000

    # RabbitMQ (event bus)
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"
    events_exchange: str = "hms.events"

    # CORS
    frontend_origin: str = "http://localhost:3000"

    @field_validator("jwt_signing_keys", mode="before")
    @classmethod
    def _parse_jwt_signing_keys(cls, value: object) -> object:
        """Allow JWT_SIGNING_KEYS to be supplied as a JSON string env var,
        e.g. JWT_SIGNING_KEYS={"k1": "base64-secret"}."""
        if isinstance(value, str):
            return json.loads(value)
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
