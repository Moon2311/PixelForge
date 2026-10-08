"""Cache key naming.

Every key is ``<CACHE_KEY_PREFIX>:<part>:<part>...``, for example::

    build_key("product", 42)                    pixelforge:product:42
    build_key("catalog", "categories")          pixelforge:catalog:categories
    build_key("orders", "recent", user_id=7)    pixelforge:user:7:orders:recent
    lock_key("pixelforge:product:42")           pixelforge:lock:product:42

Keys are deterministic. A part that isn't a plain token (letters, digits,
``_ . -``, at most 64 characters) is replaced by ``#`` + its SHA-256, so
free text such as a search query can't inject ``:`` and make two different
part lists produce the same key. Data that differs per user must pass
``user_id`` so one user's entry can never be served to another.
"""

import hashlib
import re

from django.conf import settings

_TOKEN = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def _part(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise TypeError(f"Cache key parts must be str or int, not {type(value).__name__}")
    text = str(value)
    if _TOKEN.match(text):
        return text
    return "#" + hashlib.sha256(text.encode()).hexdigest()


def build_key(*parts, user_id=None):
    if not parts:
        raise ValueError("A cache key needs at least one part")
    scope = ["user", _part(user_id)] if user_id is not None else []
    return ":".join([settings.CACHE_KEY_PREFIX, *scope, *map(_part, parts)])


def lock_key(key):
    """The rebuild-lock key that guards cache ``key``."""
    prefix = settings.CACHE_KEY_PREFIX + ":"
    name = key[len(prefix):] if key.startswith(prefix) else key
    return f"{prefix}lock:{name}"
