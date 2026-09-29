"""Backward-compatible serializers for legacy inventory endpoints.

These serializers adapt the new PostgreSQL-backed models to the format
expected by the legacy API consumers. They provide field mapping and
computed fields to maintain backward compatibility.
"""

import logging
from rest_framework import serializers

from apps.catalog.models import InventoryLog, LowStockAlert, ProductVariant

logger = logging.getLogger("catalog.inventory")


class LegacyInventoryLogSerializer(serializers.ModelSerializer):
    """Serializer for inventory logs that maps new fields to legacy format.

    New model fields:
        - variant (FK to ProductVariant)
        - previous_quantity
        - new_quantity
        - quantity_change
        - action
        - reason
        - actor_user_id

    Legacy format fields:
        - product_id (from variant.product_id)
        - product_name (from variant.product.name)
        - sku (from variant.sku)
        - quantity_before (from previous_quantity)
        - quantity_after (from new_quantity)
        - note (from reason)
        - actor (from actor_user_id)
    """

    action_label = serializers.CharField(source="get_action_display", read_only=True)

    # Legacy fields
    product_id = serializers.SerializerMethodField()
    product_name = serializers.SerializerMethodField()
    sku = serializers.SerializerMethodField()
    quantity_before = serializers.IntegerField(source="previous_quantity", read_only=True)
    quantity_after = serializers.IntegerField(source="new_quantity", read_only=True)
    note = serializers.CharField(source="reason", read_only=True)
    actor = serializers.SerializerMethodField()

    class Meta:
        model = InventoryLog
        fields = [
            "id",
            "product_id",
            "product_name",
            "sku",
            "action",
            "action_label",
            "quantity_before",
            "quantity_after",
            "quantity_change",
            "note",
            "actor",
            "created_at",
        ]

    def get_product_id(self, obj):
        """Get product_id from variant."""
        if obj.variant:
            return obj.variant.product_id
        return None

    def get_product_name(self, obj):
        """Get product name from variant."""
        if obj.variant and obj.variant.product:
            return obj.variant.product.name
        return ""

    def get_sku(self, obj):
        """Get SKU from variant."""
        if obj.variant:
            return obj.variant.sku
        return ""

    def get_actor(self, obj):
        """Get actor identifier from actor_user_id."""
        if obj.actor_user_id:
            return str(obj.actor_user_id)
        return ""


class LegacyLowStockAlertSerializer(serializers.ModelSerializer):
    """Serializer for low stock alerts that maps new fields to legacy format.

    New model fields:
        - variant (FK to ProductVariant)
        - threshold
        - quantity_at_alert
        - status (open/resolved)
        - resolved_at
        - resolved_by

    Legacy format fields:
        - product_id (from variant.product_id)
        - product_name (from variant.product.name)
        - sku (from variant.sku)
        - quantity (from quantity_at_alert)
        - min_stock_alert (from threshold)
        - resolved (from status)
    """

    # Legacy fields
    product_id = serializers.SerializerMethodField()
    product_name = serializers.SerializerMethodField()
    sku = serializers.SerializerMethodField()
    quantity = serializers.IntegerField(source="quantity_at_alert", read_only=True)
    min_stock_alert = serializers.IntegerField(source="threshold", read_only=True)
    resolved = serializers.SerializerMethodField()

    class Meta:
        model = LowStockAlert
        fields = [
            "id",
            "product_id",
            "product_name",
            "sku",
            "quantity",
            "min_stock_alert",
            "resolved",
            "created_at",
            "resolved_at",
        ]

    def get_product_id(self, obj):
        """Get product_id from variant."""
        if obj.variant:
            return obj.variant.product_id
        return None

    def get_product_name(self, obj):
        """Get product name from variant."""
        if obj.variant and obj.variant.product:
            return obj.variant.product.name
        return ""

    def get_sku(self, obj):
        """Get SKU from variant."""
        if obj.variant:
            return obj.variant.sku
        return ""

    def get_resolved(self, obj):
        """Map status to boolean resolved field."""
        return obj.status == LowStockAlert.STATUS_RESOLVED
