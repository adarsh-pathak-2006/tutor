"""Login lockout (quality backlog Q3): too many wrong passwords for one email/mobile blocks logins for a while.

Keyed by a hash of the identifier, never the identifier itself, and counted whether or not the account
exists, so the lockout can't be used to discover accounts. Resetting the password (proof of owning the
contact) or waiting out the window unlocks it. Settings: ``TUTOR_LOGIN_LOCKOUT``.
"""

import hashlib
import threading

from django.conf import settings
from django.core.cache import cache

_lock = threading.Lock()


def _key(identifier: str) -> str:
    digest = hashlib.sha256(identifier.strip().lower().encode()).hexdigest()
    return f"login-failures:{digest}"


def _config():
    return settings.TUTOR_LOGIN_LOCKOUT["max_failures"], settings.TUTOR_LOGIN_LOCKOUT["window_seconds"]


def failures(identifier: str) -> int:
    return cache.get(_key(identifier), 0)


def is_locked(identifier: str) -> bool:
    max_failures, _ = _config()
    return failures(identifier) >= max_failures


def register_failure(identifier: str) -> int:
    """Count one wrong password. The window restarts on each failure, so a steady attack stays locked out."""
    _, window = _config()
    key = _key(identifier)
    with _lock:
        count = failures(identifier) + 1
        cache.set(key, count, window)
        return count


def clear(identifier: str) -> None:
    cache.delete(_key(identifier))
