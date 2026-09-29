"""Cart API tests: authenticated cart, guest cart, merge, product validation.

Product validation goes through ``apps.catalog.selectors`` in-process; the
tests assert no outbound HTTP request is made.
"""

import socket
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from apps.authentication.models import Role, UserProfile
from apps.authentication.tokens import issue_access_token
from apps.cart.models import Cart, CartItem, GuestCart
from apps.catalog.models import Brand, Category, Inventory, Product, ProductVariant, VariantPrice


def _make_product(sku, status="active"):
    brand, _ = Brand.objects.get_or_create(name="Apple", defaults={"slug": "apple"})
    category, _ = Category.objects.get_or_create(name="Phones", defaults={"slug": "phones"})
    product = Product.objects.create(
        name=f"Phone {sku}", sku=sku, slug=sku.lower(), brand=brand, category=category, status=status,
    )
    variant = ProductVariant.objects.create(product=product, sku=f"{sku}-V")
    VariantPrice.objects.create(variant=variant, regular_price=Decimal("100.00"))
    Inventory.objects.create(variant=variant, stock_quantity=5)
    return product


def _buyer(username="buyer"):
    user = User.objects.create_user(username, f"{username}@example.com", "Buyer-pass-123!")
    UserProfile.objects.create(user=user, role=Role.objects.get_or_create(name="buyer")[0])
    return user


def _no_network(*args, **kwargs):
    raise AssertionError("Cart must not make network calls")


@mock.patch.object(socket, "create_connection", _no_network)
class AuthenticatedCartTest(TestCase):
    def setUp(self):
        self.user = _buyer()
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_access_token(self.user)}")
        self.product = _make_product("P1")

    def test_requires_authentication(self):
        self.assertEqual(APIClient().get("/api/cart/").status_code, 401)

    def test_get_creates_empty_cart(self):
        resp = self.api.get("/api/cart/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["user_id"], self.user.pk)
        self.assertEqual(data["items"], [])

    def test_add_update_remove_item(self):
        resp = self.api.post("/api/cart/items/", {"product_id": self.product.pk, "quantity": 2}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        item_id = resp.json()["data"]["id"]

        resp = self.api.post("/api/cart/items/", {"product_id": self.product.pk, "quantity": 1}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["quantity"], 3)

        resp = self.api.patch(f"/api/cart/items/{item_id}/", {"quantity": 7}, format="json")
        self.assertEqual(resp.json()["data"]["quantity"], 7)

        self.assertEqual(self.api.delete(f"/api/cart/items/{item_id}/").status_code, 200)
        self.assertFalse(CartItem.objects.exists())

    def test_unknown_and_deleted_products_are_rejected(self):
        resp = self.api.post("/api/cart/items/", {"product_id": 999999, "quantity": 1}, format="json")
        self.assertEqual(resp.status_code, 404)

        self.product.soft_delete()
        resp = self.api.post("/api/cart/items/", {"product_id": self.product.pk, "quantity": 1}, format="json")
        self.assertEqual(resp.status_code, 404)

    def test_cannot_touch_another_users_item(self):
        other_cart = Cart.objects.create(user_id=_buyer("other").pk)
        item = CartItem.objects.create(cart=other_cart, product_id=self.product.pk, quantity=1)
        resp = self.api.patch(f"/api/cart/items/{item.pk}/", {"quantity": 2}, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_clear_cart(self):
        self.api.post("/api/cart/items/", {"product_id": self.product.pk, "quantity": 1}, format="json")
        resp = self.api.delete("/api/cart/")
        self.assertEqual(resp.json()["data"]["items"], [])


@mock.patch.object(socket, "create_connection", _no_network)
class GuestCartTest(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.api.credentials(HTTP_X_GUEST_SESSION="guest-123")
        self.product = _make_product("G1")

    def _add(self, quantity=1, product_id=None):
        return self.api.post(
            "/api/cart/guest/items/",
            {
                "product_id": product_id or self.product.pk,
                "product_name": "Phone",
                "unit_price": "100.00",
                "quantity": quantity,
            },
            format="json",
        )

    def test_session_header_required(self):
        self.assertEqual(APIClient().get("/api/cart/guest/").status_code, 400)

    def test_guest_cart_flow(self):
        self.assertEqual(self._add(2).status_code, 201)
        self.assertEqual(self._add(1).status_code, 200)
        data = self.api.get("/api/cart/guest/").json()["data"]
        self.assertEqual(data["item_count"], 1)
        self.assertEqual(data["items"][0]["quantity"], 3)
        self.assertEqual(data["total"], "300.00")

        item_id = data["items"][0]["id"]
        self.api.patch(f"/api/cart/guest/items/{item_id}/", {"quantity": 0}, format="json")
        self.assertEqual(self.api.get("/api/cart/guest/").json()["data"]["item_count"], 0)

    def test_unknown_product_rejected(self):
        self.assertEqual(self._add(product_id=424242).status_code, 404)

    def test_merge_into_user_cart(self):
        self._add(3)
        user = _buyer()
        CartItem.objects.create(cart=Cart.objects.create(user_id=user.pk), product_id=self.product.pk, quantity=98)

        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_access_token(user)}")
        resp = api.post("/api/cart/merge/", {"session_id": "guest-123"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["items"][0]["quantity"], 99)
        self.assertFalse(GuestCart.objects.filter(session_id="guest-123").exists())
