"""Inventory manager for transaction-safe stock operations.

Provides a unified interface for stock updates that:
    1. Validates request
    2. Starts database transaction
    3. Locks relevant inventory row (SELECT FOR UPDATE)
    4. Updates inventory with negative-inventory guard
    5. Creates InventoryLog
    6. Creates LowStockAlert if necessary
    7. Commits transaction

Prevents race conditions via row-level locking.
Prevents negative available inventory.
"""

import logging
from decimal import Decimal
from typing import Optional

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.catalog.models import (
    Inventory,
    InventoryLog,
    LowStockAlert,
    ProductVariant,
)

logger = logging.getLogger("catalog.inventory")


class InventoryError(Exception):
    """Base exception for inventory operations."""

    pass


class InsufficientStockError(InventoryError):
    """Raised when stock adjustment would result in negative inventory."""

    pass


class VariantNotFoundError(InventoryError):
    """Raised when variant is not found."""

    pass


class InventoryManager:
    """Manages inventory operations with transaction safety and row locking.

    All public methods follow the transaction pattern:
    validate -> lock -> update -> log -> alert -> commit
    """

    # ------------------------------------------------------------------
    # Core stock adjustment
    # ------------------------------------------------------------------

    @staticmethod
    def adjust_stock(
        variant_id: int,
        action: str,
        quantity: int,
        reason: str = "",
        actor_user_id: Optional[int] = None,
    ) -> Inventory:
        """Adjust stock for a variant with full transaction safety.

        Args:
            variant_id: The variant to adjust
            action: One of stock_in, stock_out, adjustment, reservation, release, return
            quantity: Quantity (must be positive)
            reason: Optional reason for the adjustment
            actor_user_id: ID of the user performing the action

        Returns:
            Updated Inventory instance

        Raises:
            VariantNotFoundError: If variant doesn't exist
            InsufficientStockError: If adjustment would cause negative inventory
            InventoryError: For other validation errors
        """
        # Validate
        if quantity <= 0:
            raise InventoryError("Quantity must be positive")

        valid_actions = {
            "stock_in", "stock_out", "adjustment",
            "reservation", "release", "return",
        }
        if action not in valid_actions:
            raise InventoryError(f"Invalid action: {action}")

        with transaction.atomic():
            # Lock variant row
            try:
                variant = ProductVariant.objects.select_for_update().get(
                    pk=variant_id, is_deleted=False
                )
            except ProductVariant.DoesNotExist:
                raise VariantNotFoundError(f"Variant {variant_id} not found")

            # Get or create inventory with lock
            inventory, _ = Inventory.objects.select_for_update().get_or_create(
                variant=variant,
                defaults={
                    "stock_quantity": 0,
                    "reserved_quantity": 0,
                    "low_stock_threshold": 10,
                },
            )

            previous_stock = inventory.stock_quantity
            previous_reserved = inventory.reserved_quantity

            # Apply adjustment
            if action == "stock_in":
                inventory.stock_quantity += quantity

            elif action == "stock_out":
                new_stock = inventory.stock_quantity - quantity
                if new_stock < 0:
                    raise InsufficientStockError(
                        f"Insufficient stock: have {inventory.stock_quantity}, "
                        f"requested {quantity}"
                    )
                inventory.stock_quantity = new_stock

            elif action == "adjustment":
                new_stock = inventory.stock_quantity + quantity
                if new_stock < 0:
                    raise InsufficientStockError(
                        f"Adjustment would result in negative stock: "
                        f"{inventory.stock_quantity} + {quantity} = {new_stock}"
                    )
                inventory.stock_quantity = new_stock

            elif action == "reservation":
                available = inventory.available_quantity
                actual = min(quantity, available)
                if actual <= 0:
                    raise InsufficientStockError(
                        f"No available stock to reserve: "
                        f"stock={inventory.stock_quantity}, "
                        f"reserved={inventory.reserved_quantity}"
                    )
                inventory.reserved_quantity += actual

            elif action == "release":
                inventory.reserved_quantity = max(
                    0, inventory.reserved_quantity - quantity
                )

            elif action == "return":
                inventory.stock_quantity += quantity

            inventory.save()

            # Create inventory log
            InventoryLog.objects.create(
                variant=variant,
                previous_quantity=previous_stock,
                new_quantity=inventory.stock_quantity,
                quantity_change=inventory.stock_quantity - previous_stock,
                action=action,
                reason=reason,
                actor_user_id=actor_user_id,
            )

            # Check low stock and create alert if needed
            InventoryManager._handle_low_stock_alert(inventory)

            # Publish inventory event

            logger.info(
                f"Inventory adjusted: variant={variant_id}, action={action}, "
                f"quantity={quantity}, previous={previous_stock}, "
                f"new={inventory.stock_quantity}",
                extra={
                    "variant_id": variant_id,
                    "action": action,
                    "quantity": quantity,
                    "previous_stock": previous_stock,
                    "new_stock": inventory.stock_quantity,
                    "actor_user_id": actor_user_id,
                },
            )

        return inventory

    # ------------------------------------------------------------------
    # Product-level adapter (backward compatibility)
    # ------------------------------------------------------------------

    @staticmethod
    def update_product_stock(
        product_id: int,
        stock_quantity: Optional[int] = None,
        delta: Optional[int] = None,
        min_stock_alert: Optional[int] = None,
        note: str = "",
        actor_user_id: Optional[int] = None,
    ) -> dict:
        """Update stock for a product (adapter for legacy API).

        This method provides backward compatibility for the legacy
        /api/products/{id}/stock/ endpoint. It maps product-level
        stock changes to variant-level inventory operations.

        The product's first active variant is used as the target.

        Args:
            product_id: The product ID
            stock_quantity: Absolute stock value (mutually exclusive with delta)
            delta: Relative stock change (mutually exclusive with stock_quantity)
            min_stock_alert: Optional minimum stock alert threshold
            note: Optional note for the log
            actor_user_id: ID of the user performing the action

        Returns:
            Dict with updated product data for response
        """
        with transaction.atomic():
            # Find the product's primary variant
            variant = (
                ProductVariant.objects.select_for_update()
                .filter(product_id=product_id, is_deleted=False)
                .order_by("pk")
                .first()
            )

            if not variant:
                raise VariantNotFoundError(
                    f"No active variant found for product {product_id}"
                )

            # Get or create inventory with lock
            inventory, _ = Inventory.objects.select_for_update().get_or_create(
                variant=variant,
                defaults={
                    "stock_quantity": 0,
                    "reserved_quantity": 0,
                    "low_stock_threshold": min_stock_alert or 10,
                },
            )

            previous = inventory.stock_quantity

            # Calculate new stock
            if stock_quantity is not None:
                # Absolute set
                if stock_quantity < 0:
                    raise InventoryError("Stock cannot be negative")
                new_stock = stock_quantity
                action = InventoryLog.ACTION_UPDATE
            elif delta is not None:
                # Relative change
                new_stock = inventory.stock_quantity + delta
                if new_stock < 0:
                    raise InsufficientStockError(
                        f"Delta would result in negative stock: "
                        f"{inventory.stock_quantity} + {delta} = {new_stock}"
                    )
                action = (
                    InventoryLog.ACTION_STOCK_IN
                    if delta > 0
                    else InventoryLog.ACTION_STOCK_OUT
                )
            else:
                raise InventoryError("Provide either stock_quantity or delta")

            # Update inventory
            inventory.stock_quantity = new_stock

            # Update threshold if provided
            if min_stock_alert is not None:
                inventory.low_stock_threshold = max(0, min_stock_alert)

            inventory.save()

            # Create log
            InventoryLog.objects.create(
                variant=variant,
                previous_quantity=previous,
                new_quantity=new_stock,
                quantity_change=new_stock - previous,
                action=action,
                reason=note,
                actor_user_id=actor_user_id,
            )

            # Handle low stock alert
            InventoryManager._handle_low_stock_alert(inventory)

            # Publish event

            logger.info(
                f"Product stock updated: product={product_id}, "
                f"previous={previous}, new={new_stock}",
                extra={
                    "product_id": product_id,
                    "variant_id": variant.pk,
                    "previous": previous,
                    "new": new_stock,
                    "actor_user_id": actor_user_id,
                },
            )

        # Return data compatible with legacy response format
        return {
            "id": product_id,
            "stock_quantity": new_stock,
            "min_stock_alert": inventory.low_stock_threshold,
            "updated_at": timezone.now().isoformat(),
        }

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    @staticmethod
    def get_inventory_for_variant(variant_id: int) -> Optional[Inventory]:
        """Get inventory for a variant."""
        try:
            return Inventory.objects.get(variant_id=variant_id)
        except Inventory.DoesNotExist:
            return None

    @staticmethod
    def get_inventory_logs(
        product_id: Optional[int] = None,
        variant_id: Optional[int] = None,
        action: Optional[str] = None,
        limit: int = 200,
    ):
        """Get inventory logs with optional filters."""
        qs = InventoryLog.objects.all().select_related("variant")

        if product_id:
            qs = qs.filter(variant__product_id=product_id)
        if variant_id:
            qs = qs.filter(variant_id=variant_id)
        if action:
            qs = qs.filter(action=action)

        return qs.order_by("-created_at")[:limit]

    @staticmethod
    def get_low_stock_alerts(active_only: bool = True, limit: int = 200):
        """Get low stock alerts."""
        qs = LowStockAlert.objects.all().select_related("variant")

        if active_only:
            qs = qs.filter(status=LowStockAlert.STATUS_OPEN)

        return qs.order_by("-created_at")[:limit]

    @staticmethod
    def resolve_alert(alert_id: int, resolved_by: Optional[int] = None) -> LowStockAlert:
        """Resolve a low stock alert."""
        try:
            alert = LowStockAlert.objects.get(pk=alert_id)
        except LowStockAlert.DoesNotExist:
            raise InventoryError(f"Alert {alert_id} not found")

        if alert.status == LowStockAlert.STATUS_RESOLVED:
            raise InventoryError("Alert already resolved")

        alert.status = LowStockAlert.STATUS_RESOLVED
        alert.resolved_at = timezone.now()
        alert.resolved_by = resolved_by
        alert.save()

        return alert

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _handle_low_stock_alert(inventory: Inventory):
        """Create or resolve low stock alerts based on current inventory."""
        if inventory.is_low_stock:
            # Create alert if none exists
            exists = LowStockAlert.objects.filter(
                variant=inventory.variant,
                status=LowStockAlert.STATUS_OPEN,
            ).exists()

            if not exists:
                LowStockAlert.objects.create(
                    variant=inventory.variant,
                    threshold=inventory.low_stock_threshold,
                    quantity_at_alert=inventory.stock_quantity,
                )
        else:
            # Resolve open alerts if stock is above threshold
            LowStockAlert.objects.filter(
                variant=inventory.variant,
                status=LowStockAlert.STATUS_OPEN,
            ).update(
                status=LowStockAlert.STATUS_RESOLVED,
                resolved_at=timezone.now(),
            )
