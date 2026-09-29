"""RBAC permission tests.

Tests verify that every role has the correct access level:
    - admin: full access
    - catalog_manager: product/category/brand management
    - inventory_manager: inventory management only
    - customer: view + reviews
    - public: read-only

Every mutation endpoint must enforce server-side authorization.
"""

from unittest.mock import MagicMock

from django.test import TestCase, RequestFactory

from apps.authentication.permissions import (
    ROLE_ADMIN,
    ROLE_CATALOG_MANAGER,
    ROLE_INVENTORY_MANAGER,
    ROLE_CUSTOMER,
    IsAuthenticated,
    IsAdmin,
    IsCatalogManager,
    IsInventoryManager,
    IsCustomer,
    IsAdminOrCatalogManager,
    IsAdminOrInventoryManager,
    IsReadOnlyOrAdmin,
    IsOwnerOrReadOnly,
    IsReviewOwner,
    CanManageProducts,
    CanManageCategories,
    CanManageBrands,
    CanManageAttributes,
    CanManagePricing,
    CanManageInventory,
    CanCreateReview,
    CanModerateReviews,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(role=None, pk=1, is_authenticated=True):
    """Create a mock user with the given role."""
    user = MagicMock()
    user.pk = pk
    user.role = role
    user.is_authenticated = is_authenticated
    return user


def _make_request(method="GET", user=None):
    """Create a mock request with the given method and user."""
    request = MagicMock()
    request.method = method
    request.user = user
    return request


def _make_view():
    """Create a mock view."""
    return MagicMock()


# ---------------------------------------------------------------------------
# IsAuthenticated Tests
# ---------------------------------------------------------------------------


class IsAuthenticatedTest(TestCase):

    def test_authenticated_user(self):
        perm = IsAuthenticated()
        user = _make_user(role=ROLE_CUSTOMER)
        request = _make_request(user=user)
        self.assertTrue(perm.has_permission(request, _make_view()))

    def test_unauthenticated_user(self):
        perm = IsAuthenticated()
        user = _make_user(is_authenticated=False)
        request = _make_request(user=user)
        self.assertFalse(perm.has_permission(request, _make_view()))

    def test_anonymous_user(self):
        perm = IsAuthenticated()
        request = _make_request(user=None)
        self.assertFalse(perm.has_permission(request, _make_view()))


# ---------------------------------------------------------------------------
# IsAdmin Tests
# ---------------------------------------------------------------------------


class IsAdminTest(TestCase):

    def test_admin_access(self):
        perm = IsAdmin()
        user = _make_user(role=ROLE_ADMIN)
        request = _make_request(user=user)
        self.assertTrue(perm.has_permission(request, _make_view()))

    def test_catalog_manager_denied(self):
        perm = IsAdmin()
        user = _make_user(role=ROLE_CATALOG_MANAGER)
        request = _make_request(user=user)
        self.assertFalse(perm.has_permission(request, _make_view()))

    def test_inventory_manager_denied(self):
        perm = IsAdmin()
        user = _make_user(role=ROLE_INVENTORY_MANAGER)
        request = _make_request(user=user)
        self.assertFalse(perm.has_permission(request, _make_view()))

    def test_customer_denied(self):
        perm = IsAdmin()
        user = _make_user(role=ROLE_CUSTOMER)
        request = _make_request(user=user)
        self.assertFalse(perm.has_permission(request, _make_view()))


# ---------------------------------------------------------------------------
# Role-based Permissions Tests
# ---------------------------------------------------------------------------


class IsCatalogManagerTest(TestCase):

    def test_admin_access(self):
        perm = IsCatalogManager()
        user = _make_user(role=ROLE_ADMIN)
        request = _make_request(user=user)
        self.assertTrue(perm.has_permission(request, _make_view()))

    def test_catalog_manager_access(self):
        perm = IsCatalogManager()
        user = _make_user(role=ROLE_CATALOG_MANAGER)
        request = _make_request(user=user)
        self.assertTrue(perm.has_permission(request, _make_view()))

    def test_inventory_manager_denied(self):
        perm = IsCatalogManager()
        user = _make_user(role=ROLE_INVENTORY_MANAGER)
        request = _make_request(user=user)
        self.assertFalse(perm.has_permission(request, _make_view()))

    def test_customer_denied(self):
        perm = IsCatalogManager()
        user = _make_user(role=ROLE_CUSTOMER)
        request = _make_request(user=user)
        self.assertFalse(perm.has_permission(request, _make_view()))


class IsInventoryManagerTest(TestCase):

    def test_admin_access(self):
        perm = IsInventoryManager()
        user = _make_user(role=ROLE_ADMIN)
        request = _make_request(user=user)
        self.assertTrue(perm.has_permission(request, _make_view()))

    def test_inventory_manager_access(self):
        perm = IsInventoryManager()
        user = _make_user(role=ROLE_INVENTORY_MANAGER)
        request = _make_request(user=user)
        self.assertTrue(perm.has_permission(request, _make_view()))

    def test_catalog_manager_denied(self):
        perm = IsInventoryManager()
        user = _make_user(role=ROLE_CATALOG_MANAGER)
        request = _make_request(user=user)
        self.assertFalse(perm.has_permission(request, _make_view()))

    def test_customer_denied(self):
        perm = IsInventoryManager()
        user = _make_user(role=ROLE_CUSTOMER)
        request = _make_request(user=user)
        self.assertFalse(perm.has_permission(request, _make_view()))


class IsCustomerTest(TestCase):

    def test_admin_access(self):
        perm = IsCustomer()
        user = _make_user(role=ROLE_ADMIN)
        request = _make_request(user=user)
        self.assertTrue(perm.has_permission(request, _make_view()))

    def test_customer_access(self):
        perm = IsCustomer()
        user = _make_user(role=ROLE_CUSTOMER)
        request = _make_request(user=user)
        self.assertTrue(perm.has_permission(request, _make_view()))

    def test_unauthenticated_denied(self):
        perm = IsCustomer()
        user = _make_user(is_authenticated=False)
        request = _make_request(user=user)
        self.assertFalse(perm.has_permission(request, _make_view()))


# ---------------------------------------------------------------------------
# Composite Permissions Tests
# ---------------------------------------------------------------------------


class IsAdminOrCatalogManagerTest(TestCase):

    def test_admin_access(self):
        perm = IsAdminOrCatalogManager()
        user = _make_user(role=ROLE_ADMIN)
        request = _make_request(user=user)
        self.assertTrue(perm.has_permission(request, _make_view()))

    def test_catalog_manager_access(self):
        perm = IsAdminOrCatalogManager()
        user = _make_user(role=ROLE_CATALOG_MANAGER)
        request = _make_request(user=user)
        self.assertTrue(perm.has_permission(request, _make_view()))

    def test_inventory_manager_denied(self):
        perm = IsAdminOrCatalogManager()
        user = _make_user(role=ROLE_INVENTORY_MANAGER)
        request = _make_request(user=user)
        self.assertFalse(perm.has_permission(request, _make_view()))

    def test_customer_denied(self):
        perm = IsAdminOrCatalogManager()
        user = _make_user(role=ROLE_CUSTOMER)
        request = _make_request(user=user)
        self.assertFalse(perm.has_permission(request, _make_view()))


class IsAdminOrInventoryManagerTest(TestCase):

    def test_admin_access(self):
        perm = IsAdminOrInventoryManager()
        user = _make_user(role=ROLE_ADMIN)
        request = _make_request(user=user)
        self.assertTrue(perm.has_permission(request, _make_view()))

    def test_inventory_manager_access(self):
        perm = IsAdminOrInventoryManager()
        user = _make_user(role=ROLE_INVENTORY_MANAGER)
        request = _make_request(user=user)
        self.assertTrue(perm.has_permission(request, _make_view()))

    def test_catalog_manager_denied(self):
        perm = IsAdminOrInventoryManager()
        user = _make_user(role=ROLE_CATALOG_MANAGER)
        request = _make_request(user=user)
        self.assertFalse(perm.has_permission(request, _make_view()))

    def test_customer_denied(self):
        perm = IsAdminOrInventoryManager()
        user = _make_user(role=ROLE_CUSTOMER)
        request = _make_request(user=user)
        self.assertFalse(perm.has_permission(request, _make_view()))


class IsReadOnlyOrAdminTest(TestCase):

    def test_read_anyone(self):
        perm = IsReadOnlyOrAdmin()
        for role in [ROLE_ADMIN, ROLE_CATALOG_MANAGER, ROLE_INVENTORY_MANAGER, ROLE_CUSTOMER]:
            user = _make_user(role=role)
            request = _make_request(method="GET", user=user)
            self.assertTrue(perm.has_permission(request, _make_view()))

    def test_write_admin_only(self):
        perm = IsReadOnlyOrAdmin()
        user = _make_user(role=ROLE_ADMIN)
        request = _make_request(method="POST", user=user)
        self.assertTrue(perm.has_permission(request, _make_view()))

    def test_write_catalog_manager_denied(self):
        perm = IsReadOnlyOrAdmin()
        user = _make_user(role=ROLE_CATALOG_MANAGER)
        request = _make_request(method="POST", user=user)
        self.assertFalse(perm.has_permission(request, _make_view()))


# ---------------------------------------------------------------------------
# Object-level Permissions Tests
# ---------------------------------------------------------------------------


class IsOwnerOrReadOnlyTest(TestCase):

    def test_read_anyone(self):
        perm = IsOwnerOrReadOnly()
        user = _make_user(role=ROLE_CUSTOMER, pk=999)
        request = _make_request(method="GET", user=user)
        obj = MagicMock()
        obj.user_id = 1
        self.assertTrue(perm.has_object_permission(request, _make_view(), obj))

    def test_owner_edit(self):
        perm = IsOwnerOrReadOnly()
        user = _make_user(role=ROLE_CUSTOMER, pk=1)
        request = _make_request(method="PATCH", user=user)
        obj = MagicMock()
        obj.user_id = 1
        self.assertTrue(perm.has_object_permission(request, _make_view(), obj))

    def test_non_owner_denied(self):
        perm = IsOwnerOrReadOnly()
        user = _make_user(role=ROLE_CUSTOMER, pk=2)
        request = _make_request(method="PATCH", user=user)
        obj = MagicMock()
        obj.user_id = 1
        self.assertFalse(perm.has_object_permission(request, _make_view(), obj))


class IsReviewOwnerTest(TestCase):

    def test_admin_manage_all(self):
        perm = IsReviewOwner()
        user = _make_user(role=ROLE_ADMIN, pk=999)
        request = _make_request(method="PATCH", user=user)
        obj = MagicMock()
        obj.user_id = 1
        self.assertTrue(perm.has_object_permission(request, _make_view(), obj))

    def test_catalog_manager_manage_all(self):
        perm = IsReviewOwner()
        user = _make_user(role=ROLE_CATALOG_MANAGER, pk=999)
        request = _make_request(method="PATCH", user=user)
        obj = MagicMock()
        obj.user_id = 1
        self.assertTrue(perm.has_object_permission(request, _make_view(), obj))

    def test_owner_edit_own(self):
        perm = IsReviewOwner()
        user = _make_user(role=ROLE_CUSTOMER, pk=1)
        request = _make_request(method="PATCH", user=user)
        obj = MagicMock()
        obj.user_id = 1
        self.assertTrue(perm.has_object_permission(request, _make_view(), obj))

    def test_non_owner_denied(self):
        perm = IsReviewOwner()
        user = _make_user(role=ROLE_CUSTOMER, pk=2)
        request = _make_request(method="PATCH", user=user)
        obj = MagicMock()
        obj.user_id = 1
        self.assertFalse(perm.has_object_permission(request, _make_view(), obj))


# ---------------------------------------------------------------------------
# Action-specific Permissions Tests
# ---------------------------------------------------------------------------


class CanManageProductsTest(TestCase):

    def test_read_public(self):
        perm = CanManageProducts()
        for role in [None, ROLE_CUSTOMER, ROLE_INVENTORY_MANAGER, ROLE_CATALOG_MANAGER, ROLE_ADMIN]:
            user = _make_user(role=role) if role else _make_user(is_authenticated=False)
            request = _make_request(method="GET", user=user)
            self.assertTrue(perm.has_permission(request, _make_view()))

    def test_write_admin(self):
        perm = CanManageProducts()
        user = _make_user(role=ROLE_ADMIN)
        request = _make_request(method="POST", user=user)
        self.assertTrue(perm.has_permission(request, _make_view()))

    def test_write_catalog_manager(self):
        perm = CanManageProducts()
        user = _make_user(role=ROLE_CATALOG_MANAGER)
        request = _make_request(method="POST", user=user)
        self.assertTrue(perm.has_permission(request, _make_view()))

    def test_write_inventory_manager_denied(self):
        perm = CanManageProducts()
        user = _make_user(role=ROLE_INVENTORY_MANAGER)
        request = _make_request(method="POST", user=user)
        self.assertFalse(perm.has_permission(request, _make_view()))

    def test_write_customer_denied(self):
        perm = CanManageProducts()
        user = _make_user(role=ROLE_CUSTOMER)
        request = _make_request(method="POST", user=user)
        self.assertFalse(perm.has_permission(request, _make_view()))


class CanManageInventoryTest(TestCase):

    def test_read_staff_only(self):
        perm = CanManageInventory()
        user = _make_user(role=ROLE_ADMIN)
        request = _make_request(method="GET", user=user)
        self.assertTrue(perm.has_permission(request, _make_view()))

    def test_read_customer_denied(self):
        perm = CanManageInventory()
        user = _make_user(role=ROLE_CUSTOMER)
        request = _make_request(method="GET", user=user)
        self.assertFalse(perm.has_permission(request, _make_view()))

    def test_write_admin(self):
        perm = CanManageInventory()
        user = _make_user(role=ROLE_ADMIN)
        request = _make_request(method="POST", user=user)
        self.assertTrue(perm.has_permission(request, _make_view()))

    def test_write_inventory_manager(self):
        perm = CanManageInventory()
        user = _make_user(role=ROLE_INVENTORY_MANAGER)
        request = _make_request(method="POST", user=user)
        self.assertTrue(perm.has_permission(request, _make_view()))

    def test_write_catalog_manager_denied(self):
        perm = CanManageInventory()
        user = _make_user(role=ROLE_CATALOG_MANAGER)
        request = _make_request(method="POST", user=user)
        self.assertFalse(perm.has_permission(request, _make_view()))


class CanCreateReviewTest(TestCase):

    def test_read_public(self):
        perm = CanCreateReview()
        user = _make_user(is_authenticated=False)
        request = _make_request(method="GET", user=user)
        self.assertTrue(perm.has_permission(request, _make_view()))

    def test_create_authenticated(self):
        perm = CanCreateReview()
        user = _make_user(role=ROLE_CUSTOMER)
        request = _make_request(method="POST", user=user)
        self.assertTrue(perm.has_permission(request, _make_view()))

    def test_create_unauthenticated_denied(self):
        perm = CanCreateReview()
        user = _make_user(is_authenticated=False)
        request = _make_request(method="POST", user=user)
        self.assertFalse(perm.has_permission(request, _make_view()))


class CanModerateReviewsTest(TestCase):

    def test_admin_access(self):
        perm = CanModerateReviews()
        user = _make_user(role=ROLE_ADMIN)
        request = _make_request(method="POST", user=user)
        self.assertTrue(perm.has_permission(request, _make_view()))

    def test_catalog_manager_access(self):
        perm = CanModerateReviews()
        user = _make_user(role=ROLE_CATALOG_MANAGER)
        request = _make_request(method="POST", user=user)
        self.assertTrue(perm.has_permission(request, _make_view()))

    def test_inventory_manager_denied(self):
        perm = CanModerateReviews()
        user = _make_user(role=ROLE_INVENTORY_MANAGER)
        request = _make_request(method="POST", user=user)
        self.assertFalse(perm.has_permission(request, _make_view()))

    def test_customer_denied(self):
        perm = CanModerateReviews()
        user = _make_user(role=ROLE_CUSTOMER)
        request = _make_request(method="POST", user=user)
        self.assertFalse(perm.has_permission(request, _make_view()))
