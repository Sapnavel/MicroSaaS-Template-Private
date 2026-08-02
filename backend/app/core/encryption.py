"""Field-level PHI encryption.

Wraps `cryptography`'s Fernet (AES-128-CBC + HMAC) behind a SQLAlchemy
TypeDecorator so model definitions just declare `EncryptedString` as the
column type and get transparent encrypt-on-write / decrypt-on-read.

Deterministic hashing (`deterministic_hash`) is provided separately for
fields that need to be *matched* (phone, national ID) without being
decryptable in the dedup-matching path — see patients.phone_hash /
national_id_hash in the schema.
"""

import hashlib
import hmac

from cryptography.fernet import Fernet
from sqlalchemy import LargeBinary
from sqlalchemy.types import TypeDecorator

from app.config import settings

_fernet = Fernet(settings.phi_encryption_key.encode())


class EncryptedString(TypeDecorator):
    """Transparently encrypts/decrypts a string PHI field at rest."""

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect) -> bytes | None:
        if value is None:
            return None
        return _fernet.encrypt(value.encode("utf-8"))

    def process_result_value(self, value: bytes | None, dialect) -> str | None:
        if value is None:
            return None
        return _fernet.decrypt(bytes(value)).decode("utf-8")


def deterministic_hash(value: str) -> str:
    """HMAC-SHA256 of a normalized value, for equality matching (dedup) without
    storing the plaintext or a reversible ciphertext-as-index."""
    normalized = value.strip().lower()
    return hmac.new(settings.hmac_key.encode(), normalized.encode(), hashlib.sha256).hexdigest()
