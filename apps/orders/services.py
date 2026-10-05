"""Checkout: turn the user's cart into an order.

Prices and stock come from the catalog's public selector at checkout time;
the stock is taken through ``InventoryManager`` so every sale is logged.
"""

import secrets
from decimal import Decimal

from django.conf import settings
from django.db import transaction

from apps.cart.models import Cart
from apps.catalog.inventory import InsufficientStockError, InventoryManager
from apps.catalog.models import InventoryLog
from apps.catalog.selectors import get_product
from apps.payments.services import enabled_order_methods

from .models import Order, OrderItem

CENTS = Decimal("0.01")

# The store currently offers a single, free shipping method.
SHIPPING_METHODS = {
    "standard": {"code": "standard", "name": "Standard", "price": Decimal("0.00")},
}


class CheckoutConflict(Exception):
    """The cart can't be ordered as the shopper saw it (stock, price, cart)."""

    def __init__(self, message, problems=None):
        super().__init__(message)
        self.message = message
        self.problems = problems or []


def checkout_options():
    return {
        "shipping_methods": [
            {**method, "price": str(method["price"])} for method in SHIPPING_METHODS.values()
        ],
        "payment_methods": [
            {"code": Order.PAYMENT_COD, "name": "Cash on Delivery (COD)"},
            {
                "code": Order.PAYMENT_BANK_DEPOSIT,
                "name": "Bank Deposit",
                "instructions": settings.BANK_DEPOSIT_INSTRUCTIONS,
            },
            *[
                {"code": code, "name": dict(Order.PAYMENT_CHOICES)[code], "online": True}
                for code in enabled_order_methods()
            ],
        ],
        "pickup_location": settings.PICKUP_LOCATION,
    }


def _unit_price(product):
    price = product["discount_price"] or product["price"]
    return Decimal(str(price)).quantize(CENTS)


def _new_order_number():
    while True:
        number = f"PF-{secrets.token_hex(4).upper()}"
        if not Order.objects.filter(number=number).exists():
            return number


def place_order(user, data):
    """Create an order from ``user``'s cart, take the stock and empty the cart.

    Raises ``CheckoutConflict`` when the cart is empty, differs from what the
    shopper submitted, or a product is unavailable, out of stock or repriced.
    """
    with transaction.atomic():
        cart = Cart.objects.select_for_update().filter(user_id=user.id).first()
        cart_items = list(cart.items.all()) if cart else []
        if not cart_items:
            raise CheckoutConflict("Your cart is empty")

        submitted = {line["product_id"]: line for line in data["items"]}
        in_cart = {item.product_id: item.quantity for item in cart_items}
        if in_cart != {pid: line["quantity"] for pid, line in submitted.items()}:
            raise CheckoutConflict(
                "Your cart changed in another window. Please review your order."
            )

        lines, problems = [], []
        for item in cart_items:
            product = get_product(item.product_id)
            if product is None or product["status"] != "active":
                problems.append({"product_id": item.product_id, "reason": "unavailable",
                                 "name": product["name"] if product else ""})
                continue
            price = _unit_price(product)
            if product["stock_quantity"] < item.quantity:
                problems.append({"product_id": item.product_id, "reason": "out_of_stock",
                                 "name": product["name"],
                                 "available": max(0, product["stock_quantity"])})
            if price != submitted[item.product_id]["unit_price"].quantize(CENTS):
                problems.append({"product_id": item.product_id, "reason": "price_changed",
                                 "name": product["name"], "price": str(price)})
            lines.append((product, item.quantity, price))
        if problems:
            raise CheckoutConflict(_problem_message(problems), problems)

        subtotal = sum((price * qty for _, qty, price in lines), Decimal("0.00"))
        shipping = (
            SHIPPING_METHODS[data["shipping_method"]]["price"]
            if data["delivery_method"] == Order.DELIVERY_SHIP
            else Decimal("0.00")
        )
        total = subtotal + shipping
        if total != data["total"].quantize(CENTS):
            raise CheckoutConflict("Your order total changed. Please review your order.")

        order = Order.objects.create(
            number=_new_order_number(),
            user_id=user.id,
            status=(
                Order.STATUS_AWAITING_PAYMENT
                if data["payment_method"] == Order.PAYMENT_BANK_DEPOSIT
                or data["payment_method"] in Order.ONLINE_PAYMENT_METHODS
                else Order.STATUS_PENDING
            ),
            contact=data["contact"],
            marketing_opt_in=data["marketing_opt_in"],
            delivery_method=data["delivery_method"],
            shipping_address=data["shipping_address"],
            billing_address=data["billing_address"],
            shipping_method=data["shipping_method"],
            payment_method=data["payment_method"],
            subtotal=subtotal,
            shipping_total=shipping,
            total=total,
        )
        OrderItem.objects.bulk_create([
            OrderItem(
                order=order,
                product_id=product["id"],
                product_name=product["name"],
                sku=product["sku"],
                image_url=product["thumbnail"][:500],
                unit_price=price,
                quantity=qty,
                line_total=price * qty,
            )
            for product, qty, price in lines
        ])
        for product, qty, _ in lines:
            try:
                InventoryManager.update_product_stock(
                    product["id"], delta=-qty, note=f"Order {order.number}",
                    actor_user_id=user.id, action=InventoryLog.ACTION_SALE,
                )
            except InsufficientStockError:
                # Another order took the stock after our check.
                raise CheckoutConflict(
                    _problem_message([{"reason": "out_of_stock", "name": product["name"]}]),
                    [{"product_id": product["id"], "reason": "out_of_stock",
                      "name": product["name"]}],
                ) from None
        cart.items.all().delete()
    return order


def _problem_message(problems):
    first = problems[0]
    name = first.get("name") or "A product"
    if first["reason"] == "out_of_stock":
        message = f"{name} doesn't have enough stock for your order."
    elif first["reason"] == "price_changed":
        message = f"The price of {name} has changed."
    else:
        message = f"{name} is no longer available."
    if len(problems) > 1:
        message += f" ({len(problems) - 1} more item(s) need attention.)"
    return message + " Please review your cart."
