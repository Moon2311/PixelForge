"""Registration, login, token authentication, logout and role enforcement."""

from django.contrib.auth.models import User
from django.core import signing
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.authentication.models import Role, UserProfile
from apps.authentication.tokens import SALT, issue_access_token, user_from_access_token


def _user(username, role_name, password="Pass-word-123!", **extra):
    user = User.objects.create_user(username, f"{username}@example.com", password, **extra)
    UserProfile.objects.create(user=user, role=Role.objects.get_or_create(name=role_name)[0])
    return user


class RegisterAndLoginTest(TestCase):
    def setUp(self):
        Role.objects.get_or_create(name="buyer")

    def test_register_then_login_with_username_or_email(self):
        resp = self.client.post("/api/auth/register/buyer/", {
            "username": "ali", "email": "ali@example.com",
            "password": "Secret-123!", "confirm_password": "Secret-123!",
        }, content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["data"]["role"], "buyer")

        for identifier in ("ali", "ali@example.com"):
            resp = self.client.post("/api/auth/login/", {"username": identifier, "password": "Secret-123!"},
                                    content_type="application/json")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()["data"]
            self.assertEqual(data["user"]["username"], "ali")
            self.assertEqual(user_from_access_token(data["access_token"]).username, "ali")

    def test_register_with_blank_last_name(self):
        # The frontend splits a full name; a one-word name sends last_name "".
        resp = self.client.post("/api/auth/register/buyer/", {
            "first_name": "Talha", "last_name": "",
            "username": "talha2", "email": "talha2@example.com",
            "password": "Secret-123!", "confirm_password": "Secret-123!",
        }, content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["data"]["last_name"], "")

    def test_bad_credentials(self):
        _user("ali", "buyer")
        resp = self.client.post("/api/auth/login/", {"username": "ali", "password": "wrong"},
                                content_type="application/json")
        self.assertEqual(resp.status_code, 401)

    def test_logout(self):
        self.assertEqual(self.client.post("/api/auth/logout/").status_code, 200)


class AccessTokenTest(TestCase):
    def test_token_resolves_current_user_and_role(self):
        user = _user("mgr", "inventory_manager")
        token = issue_access_token(user)
        self.assertEqual(user_from_access_token(token), user)

        # Role changes apply immediately: the role is read from the DB.
        user.profile.role = Role.objects.get_or_create(name="admin")[0]
        user.profile.save()
        self.assertEqual(user_from_access_token(token).profile.role.name, "admin")

    def test_inactive_user_and_tampered_tokens_rejected(self):
        user = _user("gone", "buyer")
        token = issue_access_token(user)
        self.assertIsNone(user_from_access_token(token + "x"))
        self.assertIsNone(user_from_access_token(signing.TimestampSigner(key="other", salt=SALT).sign(str(user.pk))))
        user.is_active = False
        user.save()
        self.assertIsNone(user_from_access_token(token))

    @override_settings(ACCESS_TOKEN_MAX_AGE=-1)
    def test_expired_token_rejected(self):
        self.assertIsNone(user_from_access_token(issue_access_token(_user("old", "buyer"))))


class RoleEnforcementTest(TestCase):
    """RBAC is enforced on real users authenticated with their access token."""

    def client_for(self, user):
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_access_token(user)}")
        return api

    def test_catalog_writes_need_the_right_role(self):
        payload = {"name": "Nikon", "slug": "nikon"}
        self.assertEqual(APIClient().post("/api/catalog/brands/", payload, format="json").status_code, 401)
        buyer = self.client_for(_user("b", "buyer"))
        self.assertEqual(buyer.post("/api/catalog/brands/", payload, format="json").status_code, 403)
        admin = self.client_for(_user("a", "admin"))
        self.assertEqual(admin.post("/api/catalog/brands/", payload, format="json").status_code, 201)

    def test_legacy_admin_endpoints(self):
        inventory = self.client_for(_user("i", "inventory_manager"))
        self.assertEqual(inventory.post("/api/banners/", {"title": "Sale"}, format="json").status_code, 403)
        admin = self.client_for(_user("a", "admin"))
        self.assertEqual(admin.post("/api/banners/", {"title": "Sale"}, format="json").status_code, 201)

    def test_create_inventory_manager_requires_staff(self):
        Role.objects.get_or_create(name="inventory_manager")
        payload = {"username": "im", "email": "im@example.com", "password": "Secret-123!"}
        buyer = self.client_for(_user("b", "buyer"))
        self.assertEqual(buyer.post("/api/auth/create/inventory-manager/", payload, format="json").status_code, 403)
        staff = self.client_for(_user("s", "admin", is_staff=True))
        self.assertEqual(staff.post("/api/auth/create/inventory-manager/", payload, format="json").status_code, 201)
