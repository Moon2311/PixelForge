"""Checkout writes its order.placed event in the order's transaction."""

from django.core import mail
from django.test import TestCase

from apps.events.models import AuditEvent, OutboxEvent
from apps.notifications.models import Notification
from apps.orders.models import Order

from . import tests as order_tests


class OrderEventsTest(TestCase):
    # The checkout fixtures of PlaceOrderTest (not its tests).
    setUp = order_tests.PlaceOrderTest.setUp
    payload = order_tests.PlaceOrderTest.payload
    place = order_tests.PlaceOrderTest.place

    def test_placed_order_has_its_event_and_confirmation_email(self):
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.place()
        self.assertEqual(resp.status_code, 201, resp.content)
        order = Order.objects.get()
        row = OutboxEvent.objects.get(event_type="order.placed")
        self.assertEqual((row.topic, row.key, row.aggregate_id), ("order-events", order.number, order.number))
        payload = row.payload["payload"]
        self.assertEqual(payload["order_number"], order.number)
        self.assertEqual(payload["total"], "200.00")
        self.assertEqual(payload["item_count"], 3)
        self.assertNotIn("shipping_address", payload)  # identifiers, not personal data
        # Inline mode (no Kafka in tests): processed right after the commit.
        row.refresh_from_db()
        self.assertEqual(row.status, OutboxEvent.STATUS_PUBLISHED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(order.number, mail.outbox[0].subject)
        self.assertEqual(Notification.objects.get().status, Notification.STATUS_SENT)
        self.assertTrue(AuditEvent.objects.filter(event_type="order.placed", aggregate_id=order.number).exists())

    def test_rejected_checkout_writes_no_event(self):
        resp = self.place(total="999.00")  # total changed: CheckoutConflict, rolled back
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(Order.objects.exists())
        self.assertFalse(OutboxEvent.objects.exists())

    def test_stock_race_rolls_back_the_event(self):
        from unittest import mock

        from apps.catalog.inventory import InsufficientStockError

        with mock.patch("apps.orders.services.InventoryManager.update_product_stock",
                        side_effect=InsufficientStockError("taken")):
            resp = self.place()
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(OutboxEvent.objects.filter(event_type="order.placed").exists())
