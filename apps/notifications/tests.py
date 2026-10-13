import uuid
from datetime import timedelta
from smtplib import SMTPServerDisconnected
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.events.retry import TaskInProgress

from .models import Notification
from .services import deliver


def build():
    return "Subject", "Body"


class DeliverTest(TestCase):
    def send(self, key="k1", builder=build):
        return deliver(dedup_key=key, kind="test", recipient="buyer@example.com", build=builder,
                       event_id=uuid.uuid4())

    def test_sends_once_per_key(self):
        first = self.send()
        second = self.send()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual((first.pk, second.status), (second.pk, Notification.STATUS_SENT))
        self.assertEqual(Notification.objects.get().attempts, 1)

    def test_failure_is_recorded_and_raised_for_the_retry_policy(self):
        with (mock.patch("apps.notifications.services.send_mail", side_effect=SMTPServerDisconnected("gone")),
              self.assertRaises(SMTPServerDisconnected)):
            self.send()
        notification = Notification.objects.get()
        self.assertEqual(notification.status, Notification.STATUS_FAILED)
        self.assertIn("SMTPServerDisconnected", notification.last_error)
        self.send()  # the retry
        self.assertEqual(len(mail.outbox), 1)
        notification.refresh_from_db()
        self.assertEqual((notification.status, notification.attempts), (Notification.STATUS_SENT, 2))

    def test_concurrent_claim_raises_task_in_progress(self):
        Notification.objects.create(dedup_key="k1", kind="test", recipient="buyer@example.com",
                                    status=Notification.STATUS_SENDING,
                                    claimed_until=timezone.now() + timedelta(seconds=60))
        with self.assertRaises(TaskInProgress):
            self.send()
        self.assertEqual(len(mail.outbox), 0)

    def test_expired_claim_is_taken_over(self):
        # The sender died mid-send: after its claim expires the email is sent.
        Notification.objects.create(dedup_key="k1", kind="test", recipient="buyer@example.com",
                                    status=Notification.STATUS_SENDING,
                                    claimed_until=timezone.now() - timedelta(seconds=1))
        self.send()
        self.assertEqual(len(mail.outbox), 1)

    def test_body_is_never_stored(self):
        self.send(builder=lambda: ("Reset", "secret link https://x/reset?token=abc"))
        self.assertNotIn("token", str(Notification.objects.values().get()))


class LowStockAlertTest(TestCase):
    @override_settings(ALERT_EMAILS=["ops@example.com"])
    def test_low_stock_creates_one_alert_email(self):
        from apps.catalog.inventory import InventoryManager
        from apps.events.models import OutboxEvent
        from apps.orders.tests import _product

        product = _product("LS1", stock=12)
        with self.captureOnCommitCallbacks(execute=True):
            InventoryManager.update_product_stock(product.id, delta=-5)  # 7 <= threshold 10
        with self.captureOnCommitCallbacks(execute=True):
            InventoryManager.update_product_stock(product.id, delta=-1)  # alert already open
        (row,) = OutboxEvent.objects.filter(event_type="inventory.low_stock")
        self.assertEqual((row.payload["payload"]["quantity"], row.payload["payload"]["sku"]), (7, "LS1-V"))
        self.assertEqual([m.to for m in mail.outbox], [["ops@example.com"]])
        self.assertIn("LS1-V", mail.outbox[0].subject)
