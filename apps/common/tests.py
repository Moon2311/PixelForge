from django.test import TestCase

from apps.common.custom_response import CustomResponse


class HealthCheckTest(TestCase):
    def test_reports_django_database_and_redis(self):
        resp = self.client.get("/api/health/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()["data"]
        self.assertEqual(body["status"], "healthy")
        self.assertEqual(set(body["checks"]), {"django", "database", "redis"})
        self.assertEqual(body["checks"]["redis"]["status"], "disabled")  # test runner turns the cache off


class ApiRootTest(TestCase):
    def test_lists_endpoints(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["endpoints"]["health"], "/api/health/")


class CustomResponseTest(TestCase):
    def test_successful_response(self):
        resp = CustomResponse.successful_response({"id": 1}, "Saved", status=201, page_count=3, total=10)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data, {
            "message": "Saved", "status": 201, "is_success": True, "messageTypeId": 1,
            "data": {"id": 1}, "page_count": 3, "total": 10,
        })
        self.assertIsNone(CustomResponse.successful_response(message="Done").data["data"])

    def test_failed_response(self):
        resp = CustomResponse.failed_response("Not allowed", status=403)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data, {
            "message": "Not allowed", "status": 403, "is_success": False,
            "messageTypeId": 2, "data": {"response": []},
        })

    def test_failed_response_flattens_serializer_errors(self):
        errors = {
            "email": ["Enter a valid email."],
            "address": {"city": ["This field is required."]},
            "non_field_errors": ["Passwords don't match."],
        }
        resp = CustomResponse.failed_response(errors, serializer=True)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            resp.data["message"],
            "email: Enter a valid email.\naddress.city: This field is required.\nPasswords don't match.",
        )


class CustomResponseEverywhereTest(TestCase):
    """DRF's own errors, paginated lists and built-in viewset actions all
    answer in the CustomResponse shape."""

    def test_authentication_error(self):
        resp = self.client.get("/api/orders/")
        self.assertEqual(resp.status_code, 401)
        body = resp.json()
        self.assertEqual((body["is_success"], body["messageTypeId"], body["status"]), (False, 2, 401))
        self.assertTrue(body["message"])
        self.assertEqual(body["data"], {"response": []})
        self.assertIn("WWW-Authenticate", resp)

    def test_validation_error_keeps_field_errors(self):
        resp = self.client.post("/api/auth/register/buyer/", {}, content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertFalse(body["is_success"])
        self.assertIn("email", body["data"])
        self.assertIn("email: ", body["message"])

    def test_paginated_list_and_viewset_actions(self):
        from apps.catalog.models import Category

        for i in range(3):
            Category.objects.create(name=f"Cat {i}", slug=f"cat-{i}")
        body = self.client.get("/api/catalog/categories/?page_size=2").json()
        self.assertTrue(body["is_success"])
        self.assertEqual(len(body["data"]), 2)
        self.assertEqual((body["page_count"], body["count"]), (2, 3))
        self.assertIsNotNone(body["next"])

        category = Category.objects.first()
        body = self.client.get(f"/api/catalog/categories/{category.pk}/").json()
        self.assertEqual((body["is_success"], body["message"]), (True, "Success"))
        self.assertEqual(body["data"]["id"], category.pk)
