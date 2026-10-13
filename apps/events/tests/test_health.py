from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.events import health, outbox, topics
from apps.events.models import ConsumerHeartbeat, DeadLetterEvent, OutboxEvent


class EventsHealthTest(TestCase):
    def setUp(self):
        health.reset()
        self.addCleanup(health.reset)

    def test_inline_mode_is_healthy_and_has_no_kafka(self):
        data = self.client.get("/api/health/events/").json()["data"]
        self.assertEqual((data["status"], data["backend"], data["kafka"]["status"]), ("healthy", "inline", "disabled"))

    @override_settings(EVENTS_OUTBOX_MAX_AGE=30)
    def test_backlog_and_failures_are_reported_without_payloads(self):
        row = outbox.enqueue("order.placed", topic=topics.ORDER_EVENTS, aggregate_type="order",
                             aggregate_id="PF-1", payload={"secret_marker": "do-not-show"})
        OutboxEvent.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(seconds=120))
        DeadLetterEvent.objects.create(event_id=row.event_id, consumer_group="notifications", event_type="x",
                                       reason="retries_exhausted", envelope={})
        resp = self.client.get("/api/health/events/")
        data = resp.json()["data"]
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(data["status"], "degraded")
        self.assertEqual(data["outbox"]["pending"], 1)
        self.assertGreaterEqual(data["outbox"]["oldest_pending_age_seconds"], 120)
        self.assertEqual(data["dead_letters"]["pending_by_group"], {"notifications": 1})
        self.assertNotIn("do-not-show", resp.content.decode())
        # The main health check reports it as degraded, still 200.
        main = self.client.get("/api/health/")
        self.assertEqual((main.status_code, main.json()["data"]["status"]), (200, "degraded"))

    @override_settings(EVENTS_BACKEND="kafka", KAFKA_BOOTSTRAP_SERVERS="localhost:1", EVENTS_HEARTBEAT_STALE_AFTER=60)
    def test_kafka_down_and_missing_workers_are_problems(self):
        ConsumerHeartbeat.objects.create(member="pixelforge-images@h:1", role="pixelforge-images", hostname="h",
                                         pid=1, started_at=timezone.now(), last_seen=timezone.now(),
                                         stats={"counters": {"events_processed_total": 4},
                                                "tasks": {"t": {"count": 4, "failures": 1, "avg_ms": 10.0,
                                                                "max_ms": 20.0}}})
        with mock.patch("apps.events.health._kafka_view", side_effect=RuntimeError("no brokers")):
            data = self.client.get("/api/health/events/").json()["data"]
        self.assertEqual(data["kafka"]["status"], "error")
        problems = " | ".join(data["problems"])
        self.assertIn("Kafka unreachable", problems)
        self.assertIn("notifications: no live worker", problems)
        self.assertNotIn("images: no live worker", problems)
        images = data["workers"]["roles"]["pixelforge-images"]
        self.assertEqual(images["counters"]["events_processed_total"], 4)
        self.assertEqual(images["tasks"]["t"]["failure_rate"], 0.25)
