"""Notification tasks (consumer group ``notifications``).

Each task loads current state from PostgreSQL (the event only carries ids)
and sends through ``services.deliver``, keyed by the event id, so duplicate
deliveries send nothing twice.
"""

from django.conf import settings
from django.contrib.auth.models import User

from apps.authentication import emails as auth_emails
from apps.events.registry import task
from apps.events.retry import NonRetryableError
from apps.orders.models import Order

from .services import deliver, staff_recipients, user_email


def _order(number):
    order = Order.objects.filter(number=number).first()
    if order is None:
        # The outbox row commits with the order, so this can't be lag.
        raise NonRetryableError(f"order {number} not found")
    return order


def _customer_email(order):
    email = user_email(order.user_id)
    if not email and "@" in (order.contact or ""):
        email = order.contact
    return email


def _send(event, kind, recipient, build, user_id=None):
    return deliver(
        dedup_key=f"{kind}:{event.event_id}:{recipient.lower()}",
        kind=kind, recipient=recipient, build=build, user_id=user_id, event_id=event.event_id,
    )


def _order_lines(order):
    lines = [f"  {item.quantity} x {item.product_name}  {item.line_total}" for item in order.items.all()]
    return "\n".join(lines)


@task("notifications", "order.placed")
def order_placed(event):
    order = _order(event.payload["order_number"])
    recipient = _customer_email(order)
    if not recipient:
        return

    def build():
        if order.status == Order.STATUS_AWAITING_PAYMENT:
            if order.payment_method == Order.PAYMENT_BANK_DEPOSIT:
                next_step = ("Please complete the bank deposit to confirm it:\n"
                             f"{settings.BANK_DEPOSIT_INSTRUCTIONS}\n")
            else:
                next_step = "Your order is confirmed as soon as the payment goes through.\n"
        else:
            next_step = "We'll let you know when it ships.\n"
        body = (
            f"Thank you for your order {order.number}.\n\n"
            f"{_order_lines(order)}\n\n"
            f"Total: {order.total} PKR\n\n{next_step}"
        )
        return f"Your PixelForge order {order.number}", body

    _send(event, "order_placed", recipient, build, user_id=order.user_id)


@task("notifications", "payment.succeeded")
def payment_succeeded(event):
    payload = event.payload
    order = _order(payload["order_number"])
    recipient = _customer_email(order)
    if recipient:
        body = (
            f"We received your payment of {payload['amount']} {payload['currency']} for order "
            f"{order.number} (payment {payload['payment_id']}). Your order is "
            f"{order.get_status_display().lower()}.\n"
        )
        _send(event, "payment_received", recipient,
              lambda: (f"Payment received for order {order.number}", body), user_id=order.user_id)
    if payload.get("needs_refund"):
        _alert_staff(event, "payment_refund_required",
                     f"Refund required: payment {payload['payment_id']}",
                     f"Payment {payload['payment_id']} for order {order.number} was paid, but the order was "
                     f"already {order.get_status_display().lower()}. Refund it at the provider.\n")


@task("notifications", "payment.failed")
def payment_failed(event):
    payload = event.payload
    order = _order(payload["order_number"])
    if payload.get("needs_review"):
        _alert_staff(event, "payment_needs_review", f"Payment {payload['payment_id']} needs review",
                     f"Payment {payload['payment_id']} for order {order.number} was rejected and flagged "
                     f"for review: {payload.get('reason', '')}\n")
        return
    recipient = _customer_email(order)
    if recipient and order.status == Order.STATUS_AWAITING_PAYMENT:
        body = (f"Your payment for order {order.number} was not completed. You can pay again from "
                "your orders page.\n")
        _send(event, "payment_failed", recipient,
              lambda: (f"Payment for order {order.number} didn't go through", body), user_id=order.user_id)


@task("notifications", "payment.refunded")
def payment_refunded(event):
    payload = event.payload
    _alert_staff(event, "payment_refunded", f"Payment {payload['payment_id']} was reversed",
                 f"Payment {payload['payment_id']} for order {payload['order_number']} was refunded or "
                 "reversed at the provider. Review the order.\n")


@task("notifications", "inventory.low_stock")
def low_stock(event):
    payload = event.payload
    _alert_staff(event, "low_stock", f"Low stock: {payload.get('sku', '')}",
                 f"{payload.get('product_name') or payload.get('sku')} (SKU {payload.get('sku', '')}) is "
                 f"down to {payload['quantity']} (threshold {payload['threshold']}).\n")


@task("notifications", "notification.password_reset_requested")
def password_reset(event):
    user = _active_user(event.payload["user_id"])
    if user is None or not user.email:
        return
    # The reset link (a token) is generated here, never stored in the event.
    _send(event, "password_reset", user.email, lambda: auth_emails.password_reset_message(user),
          user_id=user.pk)


@task("notifications", "notification.password_changed")
def password_changed(event):
    user = _active_user(event.payload["user_id"])
    if user is None or not user.email:
        return
    _send(event, "password_changed", user.email, lambda: auth_emails.password_changed_message(user),
          user_id=user.pk)


def _active_user(user_id):
    return User.objects.filter(pk=user_id, is_active=True).first()


def _alert_staff(event, kind, subject, body):
    for email, user_id in staff_recipients():
        _send(event, kind, email, lambda: (subject, body), user_id=user_id)
