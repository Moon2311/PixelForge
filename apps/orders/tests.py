"""Checkout: placing orders from the user's cart."""

from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.authentication.models import Role, UserProfile
from apps.authentication.tokens import issue_access_token
from apps.cart.models import Cart, CartItem
from apps.catalog.models import (
    Brand, Category, Inventory, InventoryLog, Product, ProductVariant, VariantPrice,
)
from apps.orders.models import Order

ADDRESS = {
    "country": "Pakistan", "first_name": "Ali", "last_name": "Khan",
    "address1": "House 12, Street 4, G-10/2", "city": "Islamabad", "phone": "0300 1234567",
}


def _product(sku, price="100.00", sale=None, stock=5, status="active"):
    brand, _ = Brand.objects.get_or_create(name="Apple", defaults={"slug": "apple"})
    category, _ = Category.objects.get_or_create(name="Phones", defaults={"slug": "phones"})
    product = Product.objects.create(
        name=f"Phone {sku}", sku=sku, slug=sku.lower(), brand=brand, category=category, status=status,
    )
    variant = ProductVariant.objects.create(product=product, sku=f"{sku}-V")
    VariantPrice.objects.create(
        variant=variant, regular_price=Decimal(price), sale_price=Decimal(sale) if sale else None,
    )
    Inventory.objects.create(variant=variant, stock_quantity=stock)
    return product


class PlaceOrderTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("buyer", "buyer@example.com", "Buyer-pass-123!")
        UserProfile.objects.create(user=self.user, role=Role.objects.get_or_create(name="buyer")[0])
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_access_token(self.user)}")
        self.phone = _product("P1", price="100.00", sale="90.00", stock=5)
        self.case = _product("C1", price="20.00", stock=10)
        self.cart = Cart.objects.create(user_id=self.user.id)
        CartItem.objects.create(cart=self.cart, product_id=self.phone.id, quantity=2)
        CartItem.objects.create(cart=self.cart, product_id=self.case.id, quantity=1)

    def payload(self, **overrides):
        data = {
            "contact": "buyer@example.com",
            "delivery_method": "ship",
            "shipping_address": ADDRESS,
            "shipping_method": "standard",
            "billing_same_as_shipping": True,
            "payment_method": "cod",
            "items": [
                {"product_id": self.phone.id, "quantity": 2, "unit_price": "90.00"},
                {"product_id": self.case.id, "quantity": 1, "unit_price": "20.00"},
            ],
            "total": "200.00",
        }
        data.update(overrides)
        return data

    def place(self, **overrides):
        return self.api.post("/api/orders/", self.payload(**overrides), format="json")

    def stock(self, product):
        return Inventory.objects.get(variant__product=product).stock_quantity

    def test_cod_order_takes_stock_and_clears_cart(self):
        resp = self.place()
        self.assertEqual(resp.status_code, 201, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["total"], "200.00")
        self.assertEqual(data["billing_address"]["city"], "Islamabad")
        self.assertEqual([i["quantity"] for i in data["items"]], [2, 1])
        self.assertEqual(self.stock(self.phone), 3)
        self.assertEqual(self.stock(self.case), 9)
        self.assertEqual(
            InventoryLog.objects.filter(reason=f"Order {data['number']}", action="sale").count(), 2
        )
        self.assertFalse(self.cart.items.exists())

        detail = self.api.get(f"/api/orders/{data['number']}/")
        self.assertEqual(detail.status_code, 200)
        other = User.objects.create_user("other", "o@example.com", "Other-pass-123!")
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_access_token(other)}")
        self.assertEqual(api.get(f"/api/orders/{data['number']}/").status_code, 404)

    def test_pickup_with_bank_deposit_needs_billing_address(self):
        resp = self.place(delivery_method="pickup", shipping_address=None, payment_method="bank_deposit")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("billing_address", resp.json()["data"])

        resp = self.place(delivery_method="pickup", shipping_address=None,
                          payment_method="bank_deposit", billing_address=ADDRESS)
        self.assertEqual(resp.status_code, 201, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["status"], "awaiting_payment")
        self.assertIsNone(data["shipping_address"])
        self.assertEqual(data["shipping_method"], "")

    @override_settings(
        JAZZCASH_MERCHANT_ID="MC1", JAZZCASH_PASSWORD="pw", JAZZCASH_INTEGRITY_SALT="salt",
        JAZZCASH_RETURN_URL="https://shop.example/api/payments/jazzcash/callback/",
    )
    def test_online_payment_order_awaits_payment(self):
        resp = self.place(payment_method="jazzcash")
        self.assertEqual(resp.status_code, 201, resp.content)
        data = resp.json()["data"]
        self.assertEqual((data["status"], data["payment_method"]), ("awaiting_payment", "jazzcash"))
        self.assertRegex(data["number"], r"^PF-[0-9A-F]{8}$")

    @override_settings(JAZZCASH_MERCHANT_ID="", EASYPAISA_STORE_ID="")
    def test_unconfigured_online_payment_rejected(self):
        resp = self.place(payment_method="easypaisa")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("payment_method", resp.json()["data"])

    def test_different_billing_address(self):
        billing = {**ADDRESS, "city": "Lahore"}
        resp = self.place(billing_same_as_shipping=False, billing_address=billing)
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["data"]["billing_address"]["city"], "Lahore")

    def test_invalid_details_rejected(self):
        resp = self.place(contact="not-an-email", shipping_address={**ADDRESS, "phone": "123"})
        self.assertEqual(resp.status_code, 400)
        errors = resp.json()["data"]
        self.assertIn("contact", errors)
        self.assertIn("phone", errors["shipping_address"])
        self.assertEqual(self.place(contact="+92 300 1234567").status_code, 201)

    def test_discount_codes_not_supported(self):
        resp = self.place(discount_code="SAVE10")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("discount_code", resp.json()["data"])

    def test_out_of_stock_rejected_and_nothing_changes(self):
        inventory = Inventory.objects.get(variant__product=self.phone)
        inventory.stock_quantity = 1
        inventory.save()
        resp = self.place()
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["data"]["problems"][0]["reason"], "out_of_stock")
        self.assertEqual(self.stock(self.case), 10)
        self.assertEqual(self.cart.items.count(), 2)
        self.assertFalse(Order.objects.exists())

    def test_changed_price_rejected(self):
        VariantPrice.objects.filter(variant__product=self.phone).update(sale_price=Decimal("95.00"))
        resp = self.place()
        self.assertEqual(resp.status_code, 409)
        problem = resp.json()["data"]["problems"][0]
        self.assertEqual((problem["reason"], problem["price"]), ("price_changed", "95.00"))

    def test_cart_mismatch_and_empty_cart_rejected(self):
        resp = self.place(items=[{"product_id": self.phone.id, "quantity": 2, "unit_price": "90.00"}],
                          total="180.00")
        self.assertEqual(resp.status_code, 409)
        self.cart.items.all().delete()
        self.assertEqual(self.place().status_code, 409)

    def test_requires_login(self):
        self.assertEqual(APIClient().post("/api/orders/", self.payload(), format="json").status_code, 401)

    @override_settings(JAZZCASH_MERCHANT_ID="", EASYPAISA_STORE_ID="")
    def test_checkout_options(self):
        data = APIClient().get("/api/orders/checkout-options/").json()["data"]
        self.assertEqual(data["shipping_methods"], [{"code": "standard", "name": "Standard", "price": "0.00"}])
        self.assertEqual([m["code"] for m in data["payment_methods"]], ["cod", "bank_deposit"])


class OrderHistoryTest(TestCase):
    def setUp(self):
        self.phone = _product("P1", price="100.00", stock=10)
        self.buyer = self._user("buyer", role="customer", first_name="Ali", last_name="Khan")
        self.other = self._user("other", role="customer")
        self.admin = self._user("boss", role="admin")
        self.first = self._order(self.buyer, qty=2)
        self.second = self._order(self.buyer, qty=1)
        self.cancelled = self._order(self.other, qty=3, status=Order.STATUS_CANCELLED)

    def _user(self, username, role, **names):
        user = User.objects.create_user(username, f"{username}@example.com", "Pass-word-123!", **names)
        UserProfile.objects.create(user=user, role=Role.objects.get_or_create(name=role)[0])
        return user

    def _order(self, user, qty, status=Order.STATUS_PENDING):
        order = Order.objects.create(
            number=f"PF-{Order.objects.count():08d}", user_id=user.id, status=status,
            contact=user.email, delivery_method="ship", shipping_address=ADDRESS,
            billing_address=ADDRESS, shipping_method="standard", payment_method="cod",
            subtotal=Decimal(100 * qty), total=Decimal(100 * qty),
        )
        order.items.create(product_id=self.phone.id, product_name=self.phone.name, sku="P1",
                           unit_price=Decimal("100.00"), quantity=qty, line_total=Decimal(100 * qty))
        return order

    def client_for(self, user):
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_access_token(user)}")
        return api

    def test_my_orders_lists_only_own_orders_newest_first(self):
        resp = self.client_for(self.buyer).get("/api/orders/")
        self.assertEqual(resp.status_code, 200)
        numbers = [o["number"] for o in resp.json()["data"]]
        self.assertEqual(numbers, [self.second.number, self.first.number])
        self.assertEqual(resp.json()["data"][0]["items"][0]["quantity"], 1)
        self.assertEqual(APIClient().get("/api/orders/").status_code, 401)

    def test_admin_orders_with_customer_filter_and_search(self):
        api = self.client_for(self.admin)
        data = api.get("/api/orders/admin/orders/").json()["data"]
        self.assertEqual(len(data["orders"]), 3)
        self.assertIsNone(data["customer"])

        data = api.get(f"/api/orders/admin/orders/?user_id={self.buyer.id}").json()["data"]
        self.assertEqual(len(data["orders"]), 2)
        self.assertEqual(data["orders"][0]["customer"]["name"], "Ali Khan")
        self.assertEqual((data["customer"]["orders"], data["customer"]["spent"]), (2, "300.00"))

        data = api.get("/api/orders/admin/orders/?q=other").json()["data"]
        self.assertEqual([o["number"] for o in data["orders"]], [self.cancelled.number])
        data = api.get("/api/orders/admin/orders/?status=cancelled").json()["data"]
        self.assertEqual(len(data["orders"]), 1)

    def test_product_sales_history(self):
        resp = self.client_for(self.admin).get(f"/api/orders/admin/products/{self.phone.id}/sales/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        # The cancelled order is listed but not counted.
        self.assertEqual(data["summary"], {"units_sold": 3, "revenue": "300.00", "orders": 2, "customers": 1})
        self.assertEqual(len(data["sales"]), 3)
        self.assertEqual(data["sales"][-1]["customer"]["email"], "buyer@example.com")

    def test_admin_endpoints_need_admin(self):
        api = self.client_for(self.buyer)
        self.assertEqual(api.get("/api/orders/admin/orders/").status_code, 403)
        self.assertEqual(api.get(f"/api/orders/admin/products/{self.phone.id}/sales/").status_code, 403)
