"""Password hashing, TOTP (RFC 6238) and session tokens, using only the standard library."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time

_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
ROLES = ("admin", "supervisor", "operator", "accounts", "auditor")
ROLE_LABELS = {
    "admin": "Owner / Admin",
    "supervisor": "Supervisor",
    "operator": "Operator",
    "accounts": "Accounts",
    "auditor": "Auditor (read-only)",
}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, maxmem=64 * 1024 * 1024, **_SCRYPT)
    return "scrypt$%d$%d$%d$%s$%s" % (
        _SCRYPT["n"], _SCRYPT["r"], _SCRYPT["p"],
        base64.b64encode(salt).decode(), base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest_b64)
        digest = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p), dklen=len(expected), maxmem=64 * 1024 * 1024,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def password_problems(password: str, min_length: int, username: str = "") -> list[str]:
    problems = []
    if len(password) < min_length:
        problems.append(f"Use at least {min_length} characters.")
    if username and username.lower() in password.lower():
        problems.append("Don't include your username in the password.")
    if len(set(password)) < 5:
        problems.append("Use more varied characters.")
    if password.lower() in {"password1234", "123456789012", "weighbridge123", "qwertyuiop12"}:
        problems.append("That password is too common.")
    return problems


# --- TOTP -----------------------------------------------------------------

def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret_b32: str, counter: int, digits: int = 6) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def totp_now(secret_b32: str, at: float | None = None, step: int = 30) -> str:
    return _hotp(secret_b32, int((at or time.time()) // step))


def verify_totp(secret_b32: str, code: str, at: float | None = None, window: int = 1) -> bool:
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return False
    counter = int((at or time.time()) // 30)
    return any(
        hmac.compare_digest(_hotp(secret_b32, counter + drift), code)
        for drift in range(-window, window + 1)
    )


def totp_uri(secret_b32: str, username: str, issuer: str) -> str:
    from urllib.parse import quote
    label = quote(f"{issuer}:{username}")
    return f"otpauth://totp/{label}?secret={secret_b32}&issuer={quote(issuer)}&digits=6&period=30"


# --- sessions --------------------------------------------------------------

def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
