"""DRF permission classes for Product Catalog RBAC.

These permissions work with the SharedTokenAuthentication backend and the
AuthUser principal issued by the auth-service.

Roles:
    admin               - Full Product Catalog access
    catalog_manager     - Product/category/brand management
    inventory_manager   - Inventory management only
    customer            - View + create/edit own reviews
    (public)            - Browse active catalog, search

Every mutation endpoint must enforce server-side authorization.
Do not rely on frontend restrictions alone.
"""

from rest_framework.permissions import BasePermission, SAFE_METHODS


# ---------------------------------------------------------------------------
# Role constants
# ---------------------------------------------------------------------------

ROLE_ADMIN = "admin"
ROLE_CATALOG_MANAGER = "catalog_manager"
ROLE_INVENTORY_MANAGER = "inventory_manager"
ROLE_CUSTOMER = "customer"

ALL_ROLES = (ROLE_ADMIN, ROLE_CATALOG_MANAGER, ROLE_INVENTORY_MANAGER, ROLE_CUSTOMER)
STAFF_ROLES = (ROLE_ADMIN, ROLE_CATALOG_MANAGER, ROLE_INVENTORY_MANAGER)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _get_role(user):
    """Safely extract role from user."""
    return getattr(user, "role", None) if user else None


def _is_authenticated(user):
    """Check if user is authenticated."""
    return bool(user and getattr(user, "is_authenticated", False))


# ---------------------------------------------------------------------------
# Base permissions
# ---------------------------------------------------------------------------


class IsAuthenticated(BasePermission):
    """Allow access only to authenticated users."""

    message = "Authentication required."

    def has_permission(self, request, view):
        return _is_authenticated(request.user)


class IsAdmin(BasePermission):
    """Allow access only to authenticated users holding the 'admin' role."""

    message = "Admin privileges required."

    def has_permission(self, request, view):
        return _is_authenticated(request.user) and _get_role(request.user) == ROLE_ADMIN


# ---------------------------------------------------------------------------
# Role-based view permissions
# ---------------------------------------------------------------------------


class IsCatalogManager(BasePermission):
    """Allow access to catalog managers and admins.

    Can:
        - create/update/archive products
        - manage categories, subcategories, brands
        - manage images, attributes, specifications, pricing

    Cannot:
        - manage inventory directly
        - manage user accounts
    """

    message = "Catalog manager privileges required."

    def has_permission(self, request, view):
        return _is_authenticated(request.user) and _get_role(request.user) in (
            ROLE_ADMIN,
            ROLE_CATALOG_MANAGER,
        )


class IsInventoryManager(BasePermission):
    """Allow access to inventory managers and admins.

    Can:
        - view inventory
        - adjust inventory
        - view inventory logs
        - manage low-stock alerts

    Cannot:
        - modify product descriptions/categories/brands
        - create/update/delete products
    """

    message = "Inventory manager privileges required."

    def has_permission(self, request, view):
        return _is_authenticated(request.user) and _get_role(request.user) in (
            ROLE_ADMIN,
            ROLE_INVENTORY_MANAGER,
        )


class IsCustomer(BasePermission):
    """Allow access to any authenticated user (customer, staff, admin).

    Can:
        - view published catalog
        - create reviews
        - modify own eligible reviews
    """

    message = "Authentication required."

    def has_permission(self, request, view):
        return _is_authenticated(request.user)


# ---------------------------------------------------------------------------
# Composite permissions for common patterns
# ---------------------------------------------------------------------------


class IsAdminOrCatalogManager(BasePermission):
    """Allow admins and catalog managers.

    Used for: products, categories, subcategories, brands,
    images, attributes, specifications, pricing.
    """

    message = "Admin or catalog manager privileges required."

    def has_permission(self, request, view):
        return _is_authenticated(request.user) and _get_role(request.user) in (
            ROLE_ADMIN,
            ROLE_CATALOG_MANAGER,
        )


class IsAdminOrInventoryManager(BasePermission):
    """Allow admins and inventory managers.

    Used for: inventory adjustments, inventory logs, low-stock alerts.
    """

    message = "Admin or inventory manager privileges required."

    def has_permission(self, request, view):
        return _is_authenticated(request.user) and _get_role(request.user) in (
            ROLE_ADMIN,
            ROLE_INVENTORY_MANAGER,
        )


class IsReadOnlyOrAdmin(BasePermission):
    """Allow read-only access to anyone, write access to admins only.

    Used for: admin-only endpoints that should still be readable by staff.
    """

    message = "Write access requires admin privileges."

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        return _is_authenticated(request.user) and _get_role(request.user) == ROLE_ADMIN


# ---------------------------------------------------------------------------
# Object-level permissions
# ---------------------------------------------------------------------------


class IsOwnerOrReadOnly(BasePermission):
    """Allow owners to edit, others read-only.

    Checks user_id or created_by fields on the object.
    """

    message = "You can only edit your own resources."

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True

        if not _is_authenticated(request.user):
            return False

        user_id = getattr(request.user, "pk", None)

        if hasattr(obj, "user_id"):
            return obj.user_id == user_id
        if hasattr(obj, "created_by"):
            return obj.created_by == user_id

        return False


class IsReviewOwner(BasePermission):
    """Allow review owners to edit their own pending reviews.

    Admins and catalog managers can manage all reviews.
    Customers cannot approve their own reviews (handled in view).
    """

    message = "You can only edit your own reviews."

    def has_object_permission(self, request, view, obj):
        if not _is_authenticated(request.user):
            return False

        role = _get_role(request.user)

        # Admins and catalog managers can manage all reviews
        if role in (ROLE_ADMIN, ROLE_CATALOG_MANAGER):
            return True

        # Owners can edit their own reviews
        user_id = getattr(request.user, "pk", None)
        return obj.user_id == user_id


# ---------------------------------------------------------------------------
# Action-specific permissions
# ---------------------------------------------------------------------------


class CanManageProducts(BasePermission):
    """Allow creating/updating/deleting products.

    Products are the core catalog entity. Only catalog managers and admins
    should be able to create, update, or archive products.
    """

    message = "Product management privileges required."

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        return _is_authenticated(request.user) and _get_role(request.user) in (
            ROLE_ADMIN,
            ROLE_CATALOG_MANAGER,
        )


class CanManageCategories(BasePermission):
    """Allow managing categories and subcategories.

    Categories define the product taxonomy. Only catalog managers and admins.
    """

    message = "Category management privileges required."

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        return _is_authenticated(request.user) and _get_role(request.user) in (
            ROLE_ADMIN,
            ROLE_CATALOG_MANAGER,
        )


class CanManageBrands(BasePermission):
    """Allow managing brands.

    Brands are part of the catalog structure. Only catalog managers and admins.
    """

    message = "Brand management privileges required."

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        return _is_authenticated(request.user) and _get_role(request.user) in (
            ROLE_ADMIN,
            ROLE_CATALOG_MANAGER,
        )


class CanManageAttributes(BasePermission):
    """Allow managing attributes and attribute values.

    Attributes define product characteristics. Only catalog managers and admins.
    """

    message = "Attribute management privileges required."

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        return _is_authenticated(request.user) and _get_role(request.user) in (
            ROLE_ADMIN,
            ROLE_CATALOG_MANAGER,
        )


class CanManagePricing(BasePermission):
    """Allow managing variant pricing.

    Pricing is sensitive catalog data. Only catalog managers and admins.
    """

    message = "Pricing management privileges required."

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        return _is_authenticated(request.user) and _get_role(request.user) in (
            ROLE_ADMIN,
            ROLE_CATALOG_MANAGER,
        )


class CanManageInventory(BasePermission):
    """Allow managing inventory and stock.

    Inventory managers and admins can adjust stock, view logs,
    and manage low-stock alerts. Cannot modify product details.
    """

    message = "Inventory management privileges required."

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return _is_authenticated(request.user) and _get_role(request.user) in (
                ROLE_ADMIN,
                ROLE_INVENTORY_MANAGER,
            )
        return _is_authenticated(request.user) and _get_role(request.user) in (
            ROLE_ADMIN,
            ROLE_INVENTORY_MANAGER,
        )


class CanCreateReview(BasePermission):
    """Allow authenticated users to create reviews.

    Any authenticated user (customer, staff, admin) can create reviews.
    Duplicate review prevention is handled in the view.
    """

    message = "Authentication required to create reviews."

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        return _is_authenticated(request.user)


class CanModerateReviews(BasePermission):
    """Allow admins and catalog managers to approve/reject reviews.

    Customers cannot approve their own reviews (enforced in view logic).
    """

    message = "Review moderation privileges required."

    def has_permission(self, request, view):
        return _is_authenticated(request.user) and _get_role(request.user) in (
            ROLE_ADMIN,
            ROLE_CATALOG_MANAGER,
        )


# ---------------------------------------------------------------------------
# Backward-compatible aliases
# ---------------------------------------------------------------------------

# Legacy aliases that map to new permission classes
IsAdminOrCatalogManagerReadOnly = IsAdminOrCatalogManager
IsInventoryManagerOrAdmin = IsAdminOrInventoryManager
