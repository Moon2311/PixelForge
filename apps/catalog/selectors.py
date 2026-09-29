"""Read-side helpers the catalog exposes to other modules (cart, search).

Other modules import from here instead of reaching into catalog internals
or calling the catalog over HTTP.
"""

from apps.catalog import product_store


def get_product(product_id):
    """Flat product document for an active (non-deleted) product, or None."""
    try:
        return product_store.get_product(product_id)
    except product_store.ProductDoesNotExist:
        return None
