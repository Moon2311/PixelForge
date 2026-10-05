"""Read-side queries for order history: a shopper's orders, all orders for
staff, and who bought a given product.

Orders store only ``user_id``, so buyer details are looked up in bulk.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Count, Q, Sum

from .models import Order, OrderItem

ADMIN_ORDERS_LIMIT = 200
NEWEST_FIRST = ("-created_at", "-id")


def customers_by_id(user_ids):
    """``{user_id: {id, username, email, name}}`` for the given ids."""
    users = get_user_model().objects.in_bulk(set(user_ids))
    return {
        uid: {
            "id": uid,
            "username": user.username,
            "email": user.email,
            "name": user.get_full_name() or user.username,
        }
        for uid, user in users.items()
    }


def user_orders(user_id):
    return Order.objects.prefetch_related("items").filter(user_id=user_id).order_by(*NEWEST_FIRST)


def admin_orders(user_id=None, status=None, query=None, limit=ADMIN_ORDERS_LIMIT):
    orders = Order.objects.prefetch_related("items").order_by(*NEWEST_FIRST)
    if user_id:
        orders = orders.filter(user_id=user_id)
    if status:
        orders = orders.filter(status=status)
    if query:
        users = get_user_model().objects.filter(
            Q(username__icontains=query) | Q(email__icontains=query)
            | Q(first_name__icontains=query) | Q(last_name__icontains=query)
        ).values("id")
        orders = orders.filter(
            Q(number__icontains=query) | Q(contact__icontains=query) | Q(user_id__in=users)
        )
    return orders[:limit]


def customer_summary(user_id):
    """Order count and amount spent, not counting cancelled orders."""
    totals = Order.objects.filter(user_id=user_id).exclude(
        status=Order.STATUS_CANCELLED
    ).aggregate(orders=Count("id"), spent=Sum("total"))
    return {"orders": totals["orders"], "spent": str(totals["spent"] or "0.00")}


def product_sales(product_id):
    """Every order line for ``product_id``, newest first, with buyer details
    and totals (cancelled orders are listed but not counted)."""
    items = list(
        OrderItem.objects.select_related("order")
        .filter(product_id=product_id)
        .order_by("-order__created_at", "-order_id", "-id")
    )
    customers = customers_by_id(item.order.user_id for item in items)
    counted = [i for i in items if i.order.status != Order.STATUS_CANCELLED]
    return {
        "summary": {
            "units_sold": sum(i.quantity for i in counted),
            "revenue": str(sum((i.line_total for i in counted), Decimal("0.00"))),
            "orders": len({i.order_id for i in counted}),
            "customers": len({i.order.user_id for i in counted}),
        },
        "sales": [
            {
                "order_number": item.order.number,
                "order_status": item.order.status,
                "ordered_at": item.order.created_at,
                "customer": customers.get(item.order.user_id)
                or {"id": item.order.user_id, "username": "", "email": "", "name": ""},
                "contact": item.order.contact,
                "quantity": item.quantity,
                "unit_price": str(item.unit_price),
                "line_total": str(item.line_total),
            }
            for item in items
        ],
    }
