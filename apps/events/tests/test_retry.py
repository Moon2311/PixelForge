import random
import smtplib
import time

from django.db import OperationalError
from django.test import SimpleTestCase, override_settings

from apps.events.envelope import InvalidMessage
from apps.events.processing import time_limit
from apps.events.retry import (
    NonRetryableError,
    RetryableError,
    RetryPolicy,
    TaskTimeout,
    sanitize_error,
)


class BackoffTest(SimpleTestCase):
    def test_exponential_without_jitter(self):
        policy = RetryPolicy(base_delay=1, multiplier=2, max_delay=300, jitter=0)
        self.assertEqual([policy.delay(n) for n in range(1, 6)], [1, 2, 4, 8, 16])

    def test_max_delay_caps_even_with_jitter(self):
        policy = RetryPolicy(base_delay=1, multiplier=2, max_delay=10, jitter=0.5)
        self.assertTrue(all(policy.delay(20, random.Random(seed)) <= 10 for seed in range(50)))

    def test_jitter_spreads_within_bounds(self):
        policy = RetryPolicy(base_delay=4, multiplier=2, max_delay=300, jitter=0.25)
        delays = {round(policy.delay(1, random.Random(seed)), 3) for seed in range(200)}
        self.assertGreater(len(delays), 50)  # not synchronised
        self.assertTrue(all(3.0 <= d <= 5.0 for d in delays))

    @override_settings(EVENTS_RETRY_MAX_ATTEMPTS=7, EVENTS_RETRY_BASE_DELAY=0.5, EVENTS_RETRY_MAX_DELAY=9,
                       EVENTS_TASK_TIMEOUT=12, EVENTS_RETRY_JITTER=0.1,
                       EVENTS_NON_RETRYABLE_EXCEPTIONS=["decimal.InvalidOperation"])
    def test_from_settings(self):
        import decimal

        policy = RetryPolicy.from_settings(timeout=3)
        self.assertEqual((policy.max_attempts, policy.base_delay, policy.max_delay, policy.timeout),
                         (7, 0.5, 9, 3))
        self.assertFalse(policy.is_retryable(decimal.InvalidOperation()))


class ClassificationTest(SimpleTestCase):
    def setUp(self):
        self.policy = RetryPolicy()

    def test_transient_errors_are_retried(self):
        for exc in (ConnectionError(), TimeoutError(), OperationalError(), smtplib.SMTPServerDisconnected(),
                    RetryableError(), TaskTimeout("slow")):
            self.assertTrue(self.policy.is_retryable(exc), exc)

    def test_permanent_errors_are_not(self):
        refused = smtplib.SMTPRecipientsRefused({"x@example.com": (550, b"no such user")})
        for exc in (NonRetryableError(), InvalidMessage("bad"), KeyError("order_number"), refused):
            self.assertFalse(self.policy.is_retryable(exc), exc)
        # SMTPRecipientsRefused is an OSError: the non-retryable list wins.
        self.assertEqual(self.policy.category(refused), "permanent")

    def test_unknown_errors_follow_the_setting(self):
        class Weird(Exception):
            pass

        self.assertTrue(RetryPolicy(retry_unknown=True).is_retryable(Weird()))
        self.assertFalse(RetryPolicy(retry_unknown=False).is_retryable(Weird()))
        self.assertEqual(self.policy.category(Weird()), "unknown")

    def test_categories(self):
        self.assertEqual(self.policy.category(TaskTimeout("x")), "timeout")
        self.assertEqual(self.policy.category(ConnectionError()), "transient")
        self.assertEqual(self.policy.category(InvalidMessage("x")), "invalid_message")


class SanitizeTest(SimpleTestCase):
    def test_removes_emails_credentials_and_tokens(self):
        exc = RuntimeError(
            "send to buyer@example.com failed: password=hunter2 via smtp://user:secret@mail:25 "
            "token abcdefghijklmnopqrstuvwxyz0123456789ABCD"
        )
        text = sanitize_error(exc)
        for secret in ("buyer@example.com", "hunter2", "user:secret", "abcdefghijklmnopqrstuvwxyz0123456789ABCD"):
            self.assertNotIn(secret, text)
        self.assertTrue(text.startswith("RuntimeError: "))

    def test_truncates(self):
        self.assertLessEqual(len(sanitize_error(ValueError("x" * 5000))), 300)


class TimeLimitTest(SimpleTestCase):
    def test_interrupts_a_slow_task(self):
        started = time.monotonic()
        with self.assertRaises(TaskTimeout), time_limit(0.2):
            time.sleep(5)
        self.assertLess(time.monotonic() - started, 2)

    def test_fast_task_unaffected(self):
        with time_limit(1):
            pass
        with time_limit(None):
            pass
