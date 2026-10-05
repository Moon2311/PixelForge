from django.db import models


class Order(models.Model):
    """An order placed at checkout from the user's cart.

    Prices are copied from the catalog at checkout time, so later price
    changes don't alter past orders.
    """

    DELIVERY_SHIP = "ship"
    DELIVERY_PICKUP = "pickup"
    DELIVERY_CHOICES = [(DELIVERY_SHIP, "Ship"), (DELIVERY_PICKUP, "Pickup")]

    PAYMENT_COD = "cod"
    PAYMENT_BANK_DEPOSIT = "bank_deposit"
    PAYMENT_JAZZCASH = "jazzcash"
    PAYMENT_EASYPAISA = "easypaisa"
    PAYMENT_CHOICES = [
        (PAYMENT_COD, "Cash on Delivery (COD)"),
        (PAYMENT_BANK_DEPOSIT, "Bank Deposit"),
        (PAYMENT_JAZZCASH, "JazzCash"),
        (PAYMENT_EASYPAISA, "Easypaisa"),
    ]
    # Paid online through ``apps.payments``; the order waits for the payment.
    ONLINE_PAYMENT_METHODS = (PAYMENT_JAZZCASH, PAYMENT_EASYPAISA)
    # Orders with these methods may still be paid online while awaiting payment.
    PAYABLE_ONLINE_METHODS = (PAYMENT_BANK_DEPOSIT, *ONLINE_PAYMENT_METHODS)

    STATUS_PENDING = "pending"
    STATUS_AWAITING_PAYMENT = "awaiting_payment"
    STATUS_CONFIRMED = "confirmed"
    STATUS_SHIPPED = "shipped"
    STATUS_COMPLETED = "completed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_AWAITING_PAYMENT, "Awaiting payment"),
        (STATUS_CONFIRMED, "Confirmed"),
        (STATUS_SHIPPED, "Shipped"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    number = models.CharField(max_length=20, unique=True)
    user_id = models.IntegerField(db_index=True)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True
    )
    contact = models.CharField(max_length=254)
    marketing_opt_in = models.BooleanField(default=False)
    delivery_method = models.CharField(max_length=10, choices=DELIVERY_CHOICES)
    shipping_address = models.JSONField(null=True, blank=True)
    billing_address = models.JSONField()
    shipping_method = models.CharField(max_length=30, blank=True, default="")
    payment_method = models.CharField(max_length=20, choices=PAYMENT_CHOICES)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    shipping_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "apps_order"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Order {self.number}"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product_id = models.IntegerField(db_index=True)
    product_name = models.CharField(max_length=255)
    sku = models.CharField(max_length=100, blank=True, default="")
    image_url = models.URLField(max_length=500, blank=True, default="")
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    quantity = models.PositiveIntegerField()
    line_total = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        db_table = "apps_orderitem"
        ordering = ["id"]

    def __str__(self):
        return f"OrderItem(order={self.order_id}, product={self.product_id}, qty={self.quantity})"
