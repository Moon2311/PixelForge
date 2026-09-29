from django.db import models


class GuestCart(models.Model):
    """Session-based cart for unauthenticated users."""

    session_id = models.CharField(max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "apps_guestcart"
        ordering = ["-updated_at"]

    def __str__(self):
        return f"GuestCart(session={self.session_id})"


class GuestCartItem(models.Model):
    """A product in a guest cart."""

    cart = models.ForeignKey(GuestCart, on_delete=models.CASCADE, related_name="items")
    product_id = models.IntegerField(db_index=True)
    variant_id = models.IntegerField(null=True, blank=True)
    product_name = models.CharField(max_length=255)
    product_image_url = models.URLField(blank=True)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "apps_guestcartitem"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["cart", "product_id", "variant_id"],
                name="unique_guest_cart_product_variant",
            )
        ]

    def __str__(self):
        return f"GuestCartItem(cart={self.cart_id}, product={self.product_id}, qty={self.quantity})"


class Cart(models.Model):
    """One active cart per user. user_id is unique."""

    user_id = models.IntegerField(unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "apps_cart"
        ordering = ["-updated_at"]

    def __str__(self):
        return f"Cart(user_id={self.user_id})"


class CartItem(models.Model):
    """A product in a user's cart. Unique per (cart, product)."""

    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name="items")
    product_id = models.IntegerField(db_index=True)
    quantity = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "apps_cartitem"
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["cart", "product_id"], name="unique_cart_product"
            )
        ]

    def __str__(self):
        return f"CartItem(cart={self.cart_id}, product={self.product_id}, qty={self.quantity})"
