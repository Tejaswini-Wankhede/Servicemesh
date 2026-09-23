"""Password hashing and JWT issuance/verification.

Uses stdlib PBKDF2 for password storage and PyJWT for signed access tokens.
The implementation fails closed on malformed credentials and keeps the same
public security contract used by the API.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from servicemesh.core.config import get_settings

_HASH_SCHEME = "pbkdf2_sha256"
_ITERATIONS = 310_000
_SALT_BYTES = 16

def hash_password(password: str) -> str:
    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"{_HASH_SCHEME}${_ITERATIONS}${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"

def verify_password(plain: str, hashed: str) -> bool:
    try:
        scheme, iterations, salt_b64, digest_b64 = hashed.split("$", 3)
        if scheme != _HASH_SCHEME:
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode())
        expected = base64.urlsafe_b64decode(digest_b64.encode())
        actual = hashlib.pbkdf2_hmac("sha256", plain.encode(), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False

class TokenError(Exception):
    pass

def create_access_token(subject: str, role: str, extra: dict[str, Any] | None = None, expires_minutes: int | None = None) -> str:
    settings = get_settings()
    ttl = expires_minutes or settings.access_token_ttl_minutes
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject, "role": role, "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ttl)).timestamp()), "iss": "servicemesh",
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)

def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm], issuer="servicemesh")
    except Exception as exc:
        raise TokenError(str(exc)) from exc
