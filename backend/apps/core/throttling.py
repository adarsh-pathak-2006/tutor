"""Rate limits for the API (quality backlog Q3), built on django-ninja's throttle hook.

Rates live in ``settings.TUTOR_THROTTLE_RATES`` and are read on every request, so they can be tuned per
environment (and switched on in tests) without touching code. State is kept in the shared cache as a
sliding window of request timestamps. The throttle object is shared between threads, so no per-request
state is stored on it except in a thread-local.
"""

import logging
import threading
import time
import uuid

from django.conf import settings
from django.core.cache import cache
from ninja.throttling import BaseThrottle

from .http import client_ip

logger = logging.getLogger(__name__)

PERIOD_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

_local_lock = threading.Lock()

_LUA_SLIDING_WINDOW = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window_start = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local duration = tonumber(ARGV[4])
local member = ARGV[5]

redis.call('ZREMRANGEBYSCORE', key, '-inf', window_start)
local current_count = redis.call('ZCARD', key)
if current_count < limit then
    redis.call('ZADD', key, now, member)
    redis.call('EXPIRE', key, duration)
    return {1, 0}
else
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local wait_time = 1
    if #oldest > 0 then
        wait_time = math.max(1, math.ceil(duration - (now - tonumber(oldest[2]))))
    end
    return {0, wait_time}
end
"""


def _get_redis_client(backend):
    cache_obj = getattr(backend, "_cache", None) or getattr(backend, "client", None)
    if cache_obj is not None and hasattr(cache_obj, "get_client"):
        try:
            return cache_obj.get_client()
        except (AttributeError, RuntimeError, TypeError):
            return None
    return None


def parse_rate(rate: str) -> tuple[int, int]:
    """Parse a rate: "30/m" -> (30, 60), "5/10m" -> (5, 600). Raises ValueError for anything else."""
    try:
        count, period = rate.split("/")
        unit = period[-1]
        multiplier = int(period[:-1]) if period[:-1] else 1
        limit, seconds = int(count), multiplier * PERIOD_SECONDS[unit]
    except (ValueError, KeyError, IndexError):
        raise ValueError(f"Invalid rate {rate!r}; use e.g. '30/m', '10/h' or '5/10m'.") from None
    if limit < 1 or seconds < 1:
        raise ValueError(f"Invalid rate {rate!r}; count and period must be positive.")
    return limit, seconds


class ScopedRateThrottle(BaseThrottle):
    """Limit requests per client IP, or per user with ``per_user=True`` (falling back to IP when logged out)."""

    cache_format = "throttle:%(scope)s:%(ident)s"

    def __init__(self, scope: str, per_user: bool = False):
        self.scope = scope
        self.per_user = per_user
        self._state = threading.local()

    def rate(self):
        return settings.TUTOR_THROTTLE_RATES.get(self.scope)

    def get_cache_key(self, request):
        user = getattr(request, "user", None)
        if self.per_user and user is not None and user.is_authenticated:
            ident = f"user-{user.pk}"
        else:
            ident = f"ip-{client_ip(request)}"
        return self.cache_format % {"scope": self.scope, "ident": ident}

    def allow_request(self, request) -> bool:
        self._state.wait = None
        rate = self.rate()
        if not rate:
            return True
        limit, duration = parse_rate(rate)
        key = self.get_cache_key(request)
        now = time.time()

        redis_client = _get_redis_client(cache)
        if redis_client is not None:
            try:
                window_start = now - duration
                member = f"{now}:{uuid.uuid4().hex[:8]}"
                res = redis_client.eval(_LUA_SLIDING_WINDOW, 1, key, now, window_start, limit, duration, member)
                allowed = bool(res[0])
                wait_seconds = int(res[1])
                if not allowed:
                    self._state.wait = max(wait_seconds, 1)
                    return False
                return True
            except Exception as exc:
                logger.warning("Redis throttle check failed, falling back to local cache: %s", exc)

        with _local_lock:
            history = [t for t in cache.get(key, []) if t > now - duration]
            if len(history) >= limit:
                self._state.wait = max(int(duration - (now - history[-1])), 1)
                return False
            history.insert(0, now)
            cache.set(key, history, duration)
            return True

    def wait(self):
        return getattr(self._state, "wait", None)


api_throttle = ScopedRateThrottle("api", per_user=True)
login_throttle = ScopedRateThrottle("login")
signup_throttle = ScopedRateThrottle("signup")
otp_throttle = ScopedRateThrottle("otp")
consent_link_throttle = ScopedRateThrottle("consent_link")
consent_send_throttle = ScopedRateThrottle("consent_send", per_user=True)
tutor_throttle = ScopedRateThrottle("tutor", per_user=True)
