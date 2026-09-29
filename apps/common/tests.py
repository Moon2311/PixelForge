from django.test import TestCase


class HealthCheckTest(TestCase):
    def test_reports_django_and_database(self):
        resp = self.client.get("/api/health/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "healthy")
        self.assertEqual(set(body["checks"]), {"django", "database"})


class ApiRootTest(TestCase):
    def test_lists_endpoints(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["endpoints"]["health"], "/api/health/")
