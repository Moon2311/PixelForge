"""Running behind the L7 load balancer (nginx, see README "L7 load balancer").

``TrustedProxyMiddleware`` (first in MIDDLEWARE): when the TCP peer
(``REMOTE_ADDR``) is one of ``TRUSTED_PROXIES``, the client address is taken
from ``X-Forwarded-For`` (right to left, skipping trusted proxies) and
``X-Forwarded-Proto`` is kept for ``SECURE_PROXY_SSL_HEADER``. From anyone
else every forwarded header is removed, so a client talking to an instance
directly can't spoof its address (throttling) or claim HTTPS.

``RequestIDMiddleware``: every request gets a correlation id (the proxy's
``X-Request-ID`` when valid, else a new one). It is returned as
``X-Request-ID``, available as ``current_request_id()``, and copied into
the outbox events the request creates, so logs from the web instance, the
publisher and the workers can be tied together.
"""

import contextvars
import ipaddress
import re
import socket
import threading
import time
import uuid

from django.conf import settings

FORWARDED_HEADERS = (
    "HTTP_X_FORWARDED_FOR",
    "HTTP_X_FORWARDED_PROTO",
    "HTTP_X_FORWARDED_HOST",
    "HTTP_X_FORWARDED_PORT",
    "HTTP_X_REAL_IP",
    "HTTP_FORWARDED",
)

_resolved = {}  # hostname -> (expires_at, frozenset of ip addresses)
_resolve_lock = threading.Lock()


def _networks():
    """TRUSTED_PROXIES as ip networks; hostnames are resolved (cached)."""
    networks = []
    for entry in settings.TRUSTED_PROXIES:
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
            continue
        except ValueError:
            pass
        for address in _resolve(entry):
            networks.append(ipaddress.ip_network(address))
    return networks


def _resolve(hostname):
    now = time.monotonic()
    with _resolve_lock:
        cached = _resolved.get(hostname)
        if cached and cached[0] > now:
            return cached[1]
    try:
        addresses = frozenset(info[4][0] for info in socket.getaddrinfo(hostname, None))
    except OSError:
        addresses = frozenset()
    # An unresolvable name is retried sooner (the proxy may be starting).
    ttl = settings.TRUSTED_PROXY_DNS_TTL if addresses else 5
    with _resolve_lock:
        _resolved[hostname] = (now + ttl, addresses)
    return addresses


def _ip(value):
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def is_trusted(address, networks=None):
    ip = _ip(address or "")
    if ip is None:
        return False
    return any(ip in network for network in (_networks() if networks is None else networks))


def client_address(remote_addr, forwarded_for, networks):
    """The first address from the right of ``X-Forwarded-For`` that isn't a
    trusted proxy (the nearest hop a trusted proxy saw), else ``remote_addr``."""
    hops = [hop.strip() for hop in (forwarded_for or "").split(",") if hop.strip()]
    for hop in reversed(hops):
        ip = _ip(hop)
        if ip is None:
            break  # garbage from the client side: stop at the last good hop
        if not any(ip in network for network in networks):
            return str(ip)
    return remote_addr


class TrustedProxyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        meta = request.META
        remote = meta.get("REMOTE_ADDR", "")
        networks = _networks() if settings.TRUSTED_PROXIES else []
        if networks and is_trusted(remote, networks):
            meta["REMOTE_ADDR"] = client_address(remote, meta.get("HTTP_X_FORWARDED_FOR"), networks)
            meta["PIXELFORGE_PROXY_ADDR"] = remote
        else:
            for header in FORWARDED_HEADERS:
                meta.pop(header, None)
        return self.get_response(request)


_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_request_id = contextvars.ContextVar("pixelforge_request_id", default=None)


def current_request_id():
    """The correlation id of the request being handled, or None."""
    return _request_id.get()


def bind_request_id(value):
    """Set the correlation id for the current context (workers); returns a
    token for ``reset_request_id``."""
    return _request_id.set(value)


def reset_request_id(token):
    _request_id.reset(token)


class RequestIDMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.META.get("HTTP_X_REQUEST_ID", "")
        request_id = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        request.request_id = request_id
        token = _request_id.set(request_id)
        try:
            response = self.get_response(request)
        finally:
            _request_id.reset(token)
        response["X-Request-ID"] = request_id
        return response


def reset():
    """Forget resolved proxy hostnames (tests)."""
    with _resolve_lock:
        _resolved.clear()
