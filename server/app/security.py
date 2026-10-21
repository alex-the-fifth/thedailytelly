"""Pure-stdlib crypto helpers (no fastapi imports -> unit-testable anywhere)."""
import hashlib
import hmac
import os
import time

from . import config


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return salt.hex() + "$" + dk.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 200_000)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


def make_session_token(ttl: int | None = None, credential: str = "") -> str:
    exp = int(time.time()) + (ttl if ttl is not None else config.SESSION_TTL_SECONDS)
    payload = f"admin.{exp}"
    sig = hmac.new((config.secret_key() + credential).encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def check_session_token(token: str | None, credential: str = "") -> bool:
    if not token:
        return False
    parts = token.rsplit(".", 1)
    if len(parts) != 2:
        return False
    payload, sig = parts
    expected = hmac.new((config.secret_key() + credential).encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig.encode("utf-8"), expected.encode("utf-8")):
        return False
    try:
        role, exp = payload.split(".")
        return role == "admin" and int(exp) > time.time()
    except Exception:
        return False
