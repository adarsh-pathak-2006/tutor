import concurrent.futures
import os
import re

from django.conf import settings
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.accounts import services
from apps.accounts.models import User, VerificationCode
from apps.core import messaging
from apps.core.errors import ApiError

PASSWORD = getattr(settings, "TUTOR_DEMO_PASSWORD", None) or os.getenv("TUTOR_DEMO_PASSWORD") or "Test-Pass-2026"


def _create_user(email, mobile):
    user = User.objects.create_user(
        email=email,
        mobile=mobile,
        password=PASSWORD,
        full_name="OTP Concurrency User",
    )
    user.email_verified_at = timezone.now()
    user.save(update_fields=["email_verified_at"])
    return user


def _get_latest_otp(destination, purpose="login"):
    vc = VerificationCode.objects.filter(destination=destination, purpose=purpose).latest("created_at")
    msg_text = next(m["text"] for m in reversed(messaging.OUTBOX) if m["to"] == destination)
    match = re.search(r"code is (\d{6})", msg_text)
    return vc, match.group(1) if match else None


@override_settings(TUTOR_DEMO_DATA=False, PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class OtpVerificationLogicTests(TestCase):
    def setUp(self):
        messaging.OUTBOX.clear()
        self.user = _create_user("otp_logic_user@example.com", "+919800020001")

    def test_check_otp_valid_code_succeeds_and_marks_used(self):
        services.issue_otp(self.user.email, "login")
        vc, code = _get_latest_otp(self.user.email, "login")
        self.assertIsNotNone(code)

        verified = services.check_otp(self.user.email, "login", code)
        self.assertEqual(verified.pk, self.user.pk)

        vc.refresh_from_db()
        self.assertIsNotNone(vc.used_at)
        self.assertEqual(vc.attempts, 0)

        with self.assertRaises(ApiError) as ctx:
            services.check_otp(self.user.email, "login", code)
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "invalid_code")

    def test_check_otp_invalid_code_increments_attempts(self):
        services.issue_otp(self.user.email, "login")
        vc, code = _get_latest_otp(self.user.email, "login")

        with self.assertRaises(ApiError):
            services.check_otp(self.user.email, "login", "000000")

        vc.refresh_from_db()
        self.assertEqual(vc.attempts, 1)
        self.assertIsNone(vc.used_at)

    def test_check_otp_lockout_after_max_attempts(self):
        services.issue_otp(self.user.email, "login")
        vc, valid_code = _get_latest_otp(self.user.email, "login")

        for _ in range(VerificationCode.MAX_ATTEMPTS):
            with self.assertRaises(ApiError):
                services.check_otp(self.user.email, "login", "000000")

        vc.refresh_from_db()
        self.assertEqual(vc.attempts, VerificationCode.MAX_ATTEMPTS)

        with self.assertRaises(ApiError) as ctx:
            services.check_otp(self.user.email, "login", valid_code)
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "invalid_code")

        vc.refresh_from_db()
        self.assertIsNone(vc.used_at)


@override_settings(TUTOR_DEMO_DATA=False, PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class OtpConcurrencyLockoutTests(TransactionTestCase):
    def setUp(self):
        messaging.OUTBOX.clear()
        self.user = _create_user("otp_conc_user@example.com", "+919800020011")

    def test_multithreaded_burst_prevents_lockout_bypass(self):
        services.issue_otp(self.user.email, "login")
        vc, valid_code = _get_latest_otp(self.user.email, "login")
        num_threads = 20

        def call_check_wrong_otp(bad_code):
            connection.close()
            try:
                services.check_otp(self.user.email, "login", bad_code)
                return True
            except ApiError:
                return False
            finally:
                connection.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(call_check_wrong_otp, f"{i:06d}") for i in range(num_threads)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        self.assertTrue(all(r is False for r in results))

        vc.refresh_from_db()
        self.assertEqual(vc.attempts, VerificationCode.MAX_ATTEMPTS)

        with self.assertRaises(ApiError):
            services.check_otp(self.user.email, "login", valid_code)

    def test_password_reset_endpoint_concurrent_brute_force_lockout(self):
        services.issue_otp(self.user.email, "reset_password")
        vc, valid_code = _get_latest_otp(self.user.email, "reset_password")
        num_threads = 12

        def call_reset_endpoint(bad_code):
            connection.close()
            client = Client()
            try:
                resp = client.post(
                    "/api/v1/auth/password/reset",
                    {"destination": self.user.email, "code": bad_code, "new_password": "New-Password-999"},
                    content_type="application/json",
                )
                return resp.status_code
            finally:
                connection.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(call_reset_endpoint, f"{100000 + i}") for i in range(num_threads)]
            status_codes = [f.result() for f in concurrent.futures.as_completed(futures)]

        self.assertTrue(all(sc == 400 for sc in status_codes))

        vc.refresh_from_db()
        self.assertEqual(vc.attempts, VerificationCode.MAX_ATTEMPTS)

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))
