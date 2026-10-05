"""Network-target validation and lightweight in-process rate limiting."""

import asyncio
import ipaddress
import socket
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit


class PublicURLRequired(ValueError):
    pass


def _public_ip(value):
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


async def validate_public_url(url):
    """Reject credentials, local names, and every non-public resolved address."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise PublicURLRequired("Invalid URL") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise PublicURLRequired("Only public HTTP and HTTPS URLs are allowed")
    if parsed.username or parsed.password:
        raise PublicURLRequired("URLs containing credentials are not allowed")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise PublicURLRequired("Local and private network URLs are not allowed")

    if _public_ip(host):
        return url
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise PublicURLRequired("Local and private network URLs are not allowed")

    loop = asyncio.get_running_loop()
    try:
        answers = await loop.run_in_executor(
            None, lambda: socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
        )
    except socket.gaierror as exc:
        raise PublicURLRequired("URL host could not be resolved") from exc
    addresses = {answer[4][0].split("%")[0] for answer in answers}
    if not addresses or any(not _public_ip(address) for address in addresses):
        raise PublicURLRequired("Local and private network URLs are not allowed")
    return url


class SlidingWindowLimiter:
    def __init__(self, limit, window_seconds):
        self.limit = limit
        self.window_seconds = window_seconds
        self._events = defaultdict(deque)

    def allow(self, key):
        now = time.monotonic()
        events = self._events[key]
        cutoff = now - self.window_seconds
        while events and events[0] <= cutoff:
            events.popleft()
        if len(events) >= self.limit:
            return False
        events.append(now)
        return True

    def discard(self, key):
        self._events.pop(key, None)


def client_identity(request):
    """Use proxy forwarding headers only when the direct peer is loopback."""
    remote = request.remote or 'unknown'
    try:
        trusted_proxy = ipaddress.ip_address(remote).is_loopback
    except ValueError:
        trusted_proxy = False
    if trusted_proxy:
        forwarded = (request.headers.get('CF-Connecting-IP') or
                     request.headers.get('X-Real-IP'))
        if forwarded:
            try:
                return str(ipaddress.ip_address(forwarded.strip()))
            except ValueError:
                pass
    return remote
