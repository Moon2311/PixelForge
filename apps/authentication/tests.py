from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework.test import APITestCase
from rest_framework.throttling import ScopedRateThrottle

from .models import Role, UserProfile

FORGOT_URL = "/api/auth/forgot-password/"
RESET_URL = "/api/auth/reset-password/"
NEW_PASSWORD = "Str0ng-new-pass!"


def make_user(username, email, role_name, password="Old-pass-123!"):
    user = User.objects.create_user(username=username, email=email, password=password)
    role, _ = Role.objects.get_or_create(name=role_name)
    UserProfile.objects.create(user=user, role=role)
    return user


def reset_payload(user, **overrides):
    payload = {
        "uid": urlsafe_base64_encode(force_bytes(user.pk)),
        "token": default_token_generator.make_token(user),
        "new_password": NEW_PASSWORD,
        "confirm_password": NEW_PASSWORD,
    }
    payload.update(overrides)
    return payload


@override_settings(PASSWORD_RESET_URL="https://shop.example/reset")
class ForgotPasswordTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.buyer = make_user("buyer", "buyer@example.com", "buyer")

    def test_sends_reset_link_without_exposing_token(self):
        response = self.client.post(FORGOT_URL, {"email": "BUYER@example.com"})

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["data"])
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["buyer@example.com"])

        link = next(
            line for line in mail.outbox[0].body.splitlines()
            if line.startswith("https://shop.example/reset?")
        )
        params = parse_qs(urlparse(link).query)
        reset = self.client.post(
            RESET_URL,
            reset_payload(self.buyer, uid=params["uid"][0], token=params["token"][0]),
        )
        self.assertEqual(reset.status_code, 200)

    def test_unknown_and_non_buyer_emails_get_identical_response(self):
        make_user("manager", "manager@example.com", "inventory_manager")
        known = self.client.post(FORGOT_URL, {"email": "buyer@example.com"})
        mail.outbox.clear()

        for email in ["nobody@example.com", "manager@example.com"]:
            response = self.client.post(FORGOT_URL, {"email": email})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, known.data)
        self.assertEqual(len(mail.outbox), 0)

    def test_duplicate_emails_do_not_crash(self):
        make_user("buyer2", "buyer@example.com", "buyer")
        response = self.client.post(FORGOT_URL, {"email": "buyer@example.com"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 2)

    def test_user_without_profile_is_ignored(self):
        User.objects.create_user("noprofile", "np@example.com", "Old-pass-123!")
        response = self.client.post(FORGOT_URL, {"email": "np@example.com"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

    @mock.patch.object(
        ScopedRateThrottle, "THROTTLE_RATES", {"password_reset": "2/hour"}
    )
    def test_is_rate_limited(self):
        for _ in range(2):
            self.client.post(FORGOT_URL, {"email": "buyer@example.com"})
        response = self.client.post(FORGOT_URL, {"email": "buyer@example.com"})
        self.assertEqual(response.status_code, 429)


class ResetPasswordTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.buyer = make_user("buyer", "buyer@example.com", "buyer")

    def test_resets_password_and_notifies_user(self):
        response = self.client.post(RESET_URL, reset_payload(self.buyer))

        self.assertEqual(response.status_code, 200)
        self.buyer.refresh_from_db()
        self.assertTrue(self.buyer.check_password(NEW_PASSWORD))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("changed", mail.outbox[0].subject)

    def test_token_is_single_use(self):
        payload = reset_payload(self.buyer)
        self.assertEqual(self.client.post(RESET_URL, payload).status_code, 200)

        payload["new_password"] = payload["confirm_password"] = "An0ther-pass!"
        response = self.client.post(RESET_URL, payload)
        self.assertEqual(response.status_code, 400)
        self.assertIn("token", response.data["data"])

    def test_invalid_token_or_uid_rejected(self):
        for overrides in [{"token": "bad-token"}, {"uid": "garbage"}, {"uid": "OTk5"}]:
            response = self.client.post(RESET_URL, reset_payload(self.buyer, **overrides))
            self.assertEqual(response.status_code, 400)
            self.assertIn("token", response.data["data"])

    def test_non_buyer_rejected_with_generic_error(self):
        manager = make_user("manager", "manager@example.com", "inventory_manager")
        response = self.client.post(RESET_URL, reset_payload(manager))
        self.assertEqual(response.status_code, 400)
        self.assertIn("token", response.data["data"])

    def test_weak_password_rejected(self):
        response = self.client.post(
            RESET_URL,
            reset_payload(self.buyer, new_password="12345678", confirm_password="12345678"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("new_password", response.data["data"])

    def test_mismatched_passwords_rejected(self):
        response = self.client.post(
            RESET_URL, reset_payload(self.buyer, confirm_password="Different-pass-1!")
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("confirm_password", response.data["data"])
