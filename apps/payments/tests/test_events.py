"""Payment status events: published only after verified, committed changes;
duplicate callbacks stay harmless."""

from unittest import mock

from django.core import mail

from apps.events.models import OutboxEvent
from apps.notifications.models import Notification
from apps.orders.models import Order

from .test_payments import JC_POST, PaymentTestBase, jc_inquiry


class PaymentEventsTest(PaymentTestBase):
    def setUp(self):
        super().setUp()
        self.order.contact = "buyer@example.com"
        self.order.save(update_fields=["contact"])

    def events(self, event_type):
        return list(OutboxEvent.objects.filter(event_type=event_type))

    def test_verified_payment_publishes_success_and_notifies_the_customer(self):
        payment, _ = self.start()
        with self.captureOnCommitCallbacks(execute=True):
            self.jc_return(payment)
        (row,) = self.events("payment.succeeded")
        payload = row.payload["payload"]
        self.assertEqual((payload["payment_id"], payload["order_number"], payload["status"]),
                         (payment.payment_id, self.order.number, "PAID"))
        self.assertEqual(payload["order_status"], Order.STATUS_CONFIRMED)
        self.assertEqual(row.key, self.order.number)
        # No provider response, signature or transaction reference in the event.
        text = str(row.payload)
        for secret in ("pp_SecureHash", "220101123456", "salt123"):
            self.assertNotIn(secret, text)
        self.assertEqual([m.subject for m in mail.outbox], [f"Payment received for order {self.order.number}"])
        self.assertEqual(Notification.objects.get().kind, "payment_received")

    def test_duplicate_callbacks_publish_one_event(self):
        payment, _ = self.start()
        with self.captureOnCommitCallbacks(execute=True):
            self.jc_return(payment)
            self.jc_return(payment)  # browser refresh / provider retry
            with mock.patch(JC_POST, return_value=jc_inquiry()):
                self.status(payment)  # status polling re-checks nothing once paid
        self.assertEqual(len(self.events("payment.succeeded")), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_unverified_success_publishes_nothing(self):
        from apps.payments.services.base import ProviderError

        payment, _ = self.start()
        # Signed return, but the inquiry API is down: not trusted as paid.
        self.jc_return(payment, side_effect=ProviderError("timeout"))
        payment.refresh_from_db()
        self.assertNotEqual(payment.status, "PAID")
        self.assertEqual(self.events("payment.succeeded"), [])

    def test_failed_payment_event(self):
        payment, _ = self.start()
        with self.captureOnCommitCallbacks(execute=True):
            self.jc_return(payment, self.jc_return_fields(payment, code="004"), inquiry=jc_inquiry("004"))
        (row,) = self.events("payment.failed")
        self.assertEqual(row.payload["payload"]["previous_status"], "PENDING")
        self.assertIn("didn't go through", mail.outbox[0].subject)

    def test_amount_mismatch_alerts_staff_not_the_customer(self):
        from django.test import override_settings

        payment, _ = self.start()
        with override_settings(ALERT_EMAILS=["ops@example.com"]), self.captureOnCommitCallbacks(execute=True):
            self.jc_return(payment, self.jc_return_fields(payment, amount="100"))
        (row,) = self.events("payment.failed")
        self.assertTrue(row.payload["payload"]["needs_review"])
        self.assertEqual([m.to for m in mail.outbox], [["ops@example.com"]])
