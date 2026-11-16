import concurrent.futures
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from apps.core.throttling import ScopedRateThrottle, parse_rate


class ThrottlingConfigurationTests(SimpleTestCase):
    def test_parse_rate_valid(self):
        self.assertEqual(parse_rate("30/m"), (30, 60))
        self.assertEqual(parse_rate("10/h"), (10, 3600))
        self.assertEqual(parse_rate("5/10m"), (5, 600))
        self.assertEqual(parse_rate("100/d"), (100, 86400))
        self.assertEqual(parse_rate("5/s"), (5, 1))

    def test_parse_rate_invalid(self):
        with self.assertRaises(ValueError):
            parse_rate("invalid")
        with self.assertRaises(ValueError):
            parse_rate("0/m")
        with self.assertRaises(ValueError):
            parse_rate("10/0m")


class RedisAtomicSlidingWindowThrottleTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.request = self.factory.get("/api/v1/test", REMOTE_ADDR="198.51.100.1")
        self.throttle = ScopedRateThrottle("test_scope")

    @override_settings(TUTOR_THROTTLE_RATES={"test_scope": "5/m"})
    def test_redis_atomic_allow(self):
        mock_client = mock.MagicMock()
        mock_client.eval.return_value = [1, 0]

        with mock.patch("apps.core.throttling._get_redis_client", return_value=mock_client):
            allowed = self.throttle.allow_request(self.request)
            self.assertTrue(allowed)
            self.assertIsNone(self.throttle.wait())
            mock_client.eval.assert_called_once()
            args = mock_client.eval.call_args[0]
            self.assertEqual(args[1], 1)
            self.assertEqual(args[5], 5)
            self.assertEqual(args[6], 60)

    @override_settings(TUTOR_THROTTLE_RATES={"test_scope": "5/m"})
    def test_redis_atomic_reject(self):
        mock_client = mock.MagicMock()
        mock_client.eval.return_value = [0, 25]

        with mock.patch("apps.core.throttling._get_redis_client", return_value=mock_client):
            allowed = self.throttle.allow_request(self.request)
            self.assertFalse(allowed)
            self.assertEqual(self.throttle.wait(), 25)

    @override_settings(TUTOR_THROTTLE_RATES={"test_scope": "3/m"})
    def test_redis_exception_falls_back_gracefully(self):
        mock_client = mock.MagicMock()
        mock_client.eval.side_effect = RuntimeError("Redis connection lost")

        cache.clear()
        with mock.patch("apps.core.throttling._get_redis_client", return_value=mock_client):
            self.assertTrue(self.throttle.allow_request(self.request))
            self.assertTrue(self.throttle.allow_request(self.request))
            self.assertTrue(self.throttle.allow_request(self.request))
            self.assertFalse(self.throttle.allow_request(self.request))
            self.assertGreaterEqual(self.throttle.wait(), 1)


class ConcurrencyBurstSafetyTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()

    @override_settings(TUTOR_THROTTLE_RATES={"burst_scope": "5/m"})
    def test_multithreaded_burst_prevents_race_condition(self):
        throttle = ScopedRateThrottle("burst_scope")
        num_threads = 20
        requests = [self.factory.get("/api/v1/test", REMOTE_ADDR="203.0.113.50") for _ in range(num_threads)]

        def call_throttle(req):
            return throttle.allow_request(req)

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            results = list(executor.map(call_throttle, requests))

        allowed_count = sum(1 for r in results if r is True)
        blocked_count = sum(1 for r in results if r is False)

        self.assertEqual(allowed_count, 5)
        self.assertEqual(blocked_count, 15)

    def test_unauthenticated_and_authenticated_cache_keys(self):
        throttle = ScopedRateThrottle("user_scope", per_user=True)
        anon_req = self.factory.get("/test", REMOTE_ADDR="192.0.2.1")
        anon_req.user = SimpleNamespace(is_authenticated=False)
        self.assertIn("ip-192.0.2.1", throttle.get_cache_key(anon_req))

        auth_req = self.factory.get("/test", REMOTE_ADDR="192.0.2.1")
        auth_req.user = SimpleNamespace(is_authenticated=True, pk=123)
        self.assertIn("user-123", throttle.get_cache_key(auth_req))


class ThrottlingEndpointIntegrationTests(TestCase):
    @override_settings(TUTOR_THROTTLE_RATES={"login": "2/m"})
    def test_endpoint_returns_429_with_retry_after_header(self):
        cache.clear()
        payload = {"email": "nobody@example.com", "password": "wrong-password"}

        resp1 = self.client.post("/api/v1/auth/login", payload, content_type="application/json")
        self.assertIn(resp1.status_code, (400, 401))

        resp2 = self.client.post("/api/v1/auth/login", payload, content_type="application/json")
        self.assertIn(resp2.status_code, (400, 401))

        resp3 = self.client.post("/api/v1/auth/login", payload, content_type="application/json")
        self.assertEqual(resp3.status_code, 429)
        self.assertIn("Retry-After", resp3.headers)
        self.assertGreater(int(resp3.headers["Retry-After"]), 0)
