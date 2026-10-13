"""Forwarded headers are believed only from trusted proxies; request ids."""

from django.http import JsonResponse
from django.test import RequestFactory, SimpleTestCase, override_settings

from apps.common import proxy


def echo(request):
    return JsonResponse({
        "remote": request.META.get("REMOTE_ADDR"),
        "secure": request.is_secure(),
        "forwarded_for": request.META.get("HTTP_X_FORWARDED_FOR"),
        "request_id": proxy.current_request_id(),
    })


class TrustedProxyTest(SimpleTestCase):
    def setUp(self):
        proxy.reset()
        self.factory = RequestFactory()
        self.middleware = proxy.TrustedProxyMiddleware(echo)

    def call(self, remote, **headers):
        request = self.factory.get("/", REMOTE_ADDR=remote, **headers)
        response = self.middleware(request)
        import json

        return json.loads(response.content)

    @override_settings(TRUSTED_PROXIES=["10.0.0.0/24"], SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"))
    def test_trusted_proxy_supplies_client_address_and_scheme(self):
        data = self.call("10.0.0.5", HTTP_X_FORWARDED_FOR="203.0.113.7", HTTP_X_FORWARDED_PROTO="https")
        self.assertEqual(data["remote"], "203.0.113.7")
        self.assertTrue(data["secure"])

    @override_settings(TRUSTED_PROXIES=["10.0.0.0/24"])
    def test_client_supplied_hops_are_not_believed(self):
        # The client sent its own X-Forwarded-For; nginx appended the real peer.
        data = self.call("10.0.0.5", HTTP_X_FORWARDED_FOR="1.2.3.4, 198.51.100.9")
        self.assertEqual(data["remote"], "198.51.100.9")
        # Chains of trusted proxies are skipped from the right.
        data = self.call("10.0.0.5", HTTP_X_FORWARDED_FOR="198.51.100.9, 10.0.0.7")
        self.assertEqual(data["remote"], "198.51.100.9")

    @override_settings(TRUSTED_PROXIES=["10.0.0.0/24"], SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"))
    def test_untrusted_sender_cannot_spoof(self):
        data = self.call("198.51.100.9", HTTP_X_FORWARDED_FOR="10.9.9.9", HTTP_X_FORWARDED_PROTO="https")
        self.assertEqual(data["remote"], "198.51.100.9")
        self.assertFalse(data["secure"])
        self.assertIsNone(data["forwarded_for"])

    @override_settings(TRUSTED_PROXIES=[])
    def test_no_trusted_proxies_strips_everything(self):
        data = self.call("127.0.0.1", HTTP_X_FORWARDED_FOR="203.0.113.7")
        self.assertEqual((data["remote"], data["forwarded_for"]), ("127.0.0.1", None))

    @override_settings(TRUSTED_PROXIES=["localhost"])
    def test_hostnames_are_resolved(self):
        data = self.call("127.0.0.1", HTTP_X_FORWARDED_FOR="203.0.113.7")
        self.assertEqual(data["remote"], "203.0.113.7")


class RequestIdTest(SimpleTestCase):
    def test_valid_incoming_id_is_kept_and_returned(self):
        middleware = proxy.RequestIDMiddleware(echo)
        response = middleware(RequestFactory().get("/", HTTP_X_REQUEST_ID="abc123def456"))
        self.assertEqual(response["X-Request-ID"], "abc123def456")
        self.assertIn(b"abc123def456", response.content)
        self.assertIsNone(proxy.current_request_id())  # reset after the request

    def test_invalid_id_is_replaced(self):
        response = proxy.RequestIDMiddleware(echo)(RequestFactory().get("/", HTTP_X_REQUEST_ID="bad id\nX: y"))
        self.assertRegex(response["X-Request-ID"], r"^[0-9a-f]{32}$")
