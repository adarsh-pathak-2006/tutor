"""Secrets sent by link or SMS are stored only as hashes (DATA_MODEL.md §4)."""

import secrets

from django.utils.crypto import salted_hmac


def hash_secret(raw: str, salt: str = "tutor.accounts.secret") -> str:
    return salted_hmac(salt, raw, algorithm="sha256").hexdigest()


def make_link_token() -> tuple[str, str]:
    """Return (raw token for the link, hash to store)."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_secret(raw)


def make_otp() -> tuple[str, str]:
    """Return (6-digit code to send, hash to store)."""
    raw = f"{secrets.randbelow(10**6):06d}"
    return raw, hash_secret(raw)
