from rest_framework.views import APIView

from apps.common.custom_response import CustomResponse

from . import health


class EventsHealthView(APIView):
    """GET /api/health/events/ — outbox backlog, Kafka brokers and topics,
    consumer groups (state, members, lag), worker/publisher heartbeats with
    their counters and task latency, and dead letters awaiting review.

    Always 200 with ``status`` healthy/degraded and a ``problems`` list:
    the web instances keep working when the pipeline is degraded (events
    wait in the outbox). Counts and states only, never payloads or secrets.
    """

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        data = health.snapshot()
        return CustomResponse.successful_response(data, f"Events {data['status']}")
