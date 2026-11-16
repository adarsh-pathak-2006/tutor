import concurrent.futures
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings

from apps.accounts import lockout
from apps.core import throttling
from apps.core.throttling import ScopedRateThrottle, parse_rate


class ThrottlingScalabilityTests(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()

    def tearDown(self):
        cache.clear()

    def test_parse_rate_valid_formats(self):
        limit, seconds = parse_rate("30/m")
        self.assertEqual(limit, 30)
        self.assertEqual(seconds, 60)

        limit, seconds = parse_rate("10/s")
        self.assertEqual(limit, 10)
        self.assertEqual(seconds, 1)

        limit, seconds = parse_rate("5/10m")
        self.assertEqual(limit, 5)
        self.assertEqual(seconds, 600)

        limit, seconds = parse_rate("1/h")
        self.assertEqual(limit, 1)
        self.assertEqual(seconds, 3600)

        limit, seconds = parse_rate("100/d")
        self.assertEqual(limit, 100)
        self.assertEqual(seconds, 86400)

    def test_parse_rate_invalid_formats(self):
        invalid_rates = ["invalid", "30", "30/", "/m", "-5/m", "5/-1m", "0/m"]
        for rate in invalid_rates:
            with self.assertRaises(ValueError):
                parse_rate(rate)

    @override_settings(TUTOR_THROTTLE_RATES={"test_scope": "3/m"})
    def test_throttle_allows_under_limit_and_blocks_excess(self):
        throttle = ScopedRateThrottle("test_scope")
        request = self.factory.get("/api/v1/test", REMOTE_ADDR="198.51.100.1")

        self.assertTrue(throttle.allow_request(request))
        self.assertIsNone(throttle.wait())

        self.assertTrue(throttle.allow_request(request))
        self.assertIsNone(throttle.wait())

        self.assertTrue(throttle.allow_request(request))
        self.assertIsNone(throttle.wait())

        self.assertFalse(throttle.allow_request(request))
        self.assertIsNotNone(throttle.wait())
        self.assertGreaterEqual(throttle.wait(), 1)

    @override_settings(TUTOR_THROTTLE_RATES={"test_scope": "2/s"})
    def test_throttle_sliding_window_clears_expired(self):
        throttle = ScopedRateThrottle("test_scope")
        request = self.factory.get("/api/v1/test", REMOTE_ADDR="198.51.100.2")

        current_time = [100.0]

        def fake_time():
            return current_time[0]

        with patch("time.time", side_effect=fake_time):
            self.assertTrue(throttle.allow_request(request))
            self.assertTrue(throttle.allow_request(request))
            self.assertFalse(throttle.allow_request(request))
            current_time[0] += 5.0
            self.assertTrue(throttle.allow_request(request))

    @override_settings(TUTOR_THROTTLE_RATES={"test_scope": "5/m"})
    def test_concurrent_throttle_burst_thread_safety(self):
        throttle = ScopedRateThrottle("test_scope")
        request = self.factory.get("/api/v1/test", REMOTE_ADDR="198.51.100.3")

        def make_request(_):
            return throttle.allow_request(request)

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(make_request, range(20)))

        allowed_count = sum(1 for res in results if res is True)
        blocked_count = sum(1 for res in results if res is False)
        self.assertEqual(allowed_count, 5)
        self.assertEqual(blocked_count, 15)

    @override_settings(TUTOR_THROTTLE_RATES={"test_scope": "2/m"})
    def test_redis_lua_sliding_window_execution(self):
        throttle = ScopedRateThrottle("test_scope")
        request = self.factory.get("/api/v1/test", REMOTE_ADDR="198.51.100.4")

        mock_redis = MagicMock()
        mock_redis.eval.side_effect = [
            [1, 0],
            [1, 0],
            [0, 58],
        ]

        with patch.object(throttling, "_get_redis_client", return_value=mock_redis):
            self.assertTrue(throttle.allow_request(request))
            self.assertTrue(throttle.allow_request(request))
            self.assertFalse(throttle.allow_request(request))
            self.assertEqual(throttle.wait(), 58)

        self.assertEqual(mock_redis.eval.call_count, 3)

    @override_settings(TUTOR_THROTTLE_RATES={"test_scope": "1/m"})
    def test_redis_lua_failure_falls_back_to_local_cache(self):
        throttle = ScopedRateThrottle("test_scope")
        request = self.factory.get("/api/v1/test", REMOTE_ADDR="198.51.100.5")

        mock_redis = MagicMock()
        mock_redis.eval.side_effect = RuntimeError("Redis connection error")

        with patch.object(throttling, "_get_redis_client", return_value=mock_redis):
            self.assertTrue(throttle.allow_request(request))
            self.assertFalse(throttle.allow_request(request))

    @override_settings(TUTOR_LOGIN_LOCKOUT={"max_failures": 5, "window_seconds": 300})
    def test_lockout_register_failure_concurrency(self):
        identifier = "concurrent_attacker@example.com"
        lockout.clear(identifier)
        self.assertFalse(lockout.is_locked(identifier))

        def record_failure(_):
            return lockout.register_failure(identifier)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(record_failure, range(10)))

        self.assertEqual(lockout.failures(identifier), 10)
        self.assertTrue(lockout.is_locked(identifier))
        lockout.clear(identifier)
        self.assertEqual(lockout.failures(identifier), 0)
        self.assertFalse(lockout.is_locked(identifier))
