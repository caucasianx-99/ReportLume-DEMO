"""Single-process authentication throttling with explicit proxy trust.

Do not trust client-supplied forwarding headers. Railway's X-Real-IP is
accepted only after an external staging test confirms that the edge proxy
replaces a forged incoming value. This module does not provide distributed
rate limiting; production admission separately requires one verified worker.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import math
import secrets
import time

from collections import deque

from fastapi import HTTPException, Request

from app.core.config import settings


# Requests per sliding window, keyed to the resolved client IP.
LIMITS = {
    "signup": (5, 3600),
    "login": (10, 300),
    "reset": (5, 3600),
}

# Separate identity-based controls resist attacks spread across many IPs.
ACCOUNT_LIMITS = {
    "login": (20, 3600),
    "reset": (5, 3600),
}

MAX_IDENTITIES = 4096
MAX_ADDRESS_LENGTH = 128
_EPHEMERAL_SECRET = secrets.token_bytes(32)

_entries = {}
_lock = asyncio.Lock()


def normalize_account(email: str | None) -> str | None:
    """Return a bounded, case-insensitive identifier for auth throttling."""

    if not isinstance(email, str):
        return None

    normalized = email.strip().casefold()

    if not normalized or len(normalized) > 254:
        return None

    return normalized


def _account_digest(email: str) -> str:
    secret = getattr(settings, "auth_rate_limit_key_secret", "")

    if getattr(settings, "production", False) and len(secret) < 32:
        _temporarily_unavailable()

    key = secret.encode("utf-8") if secret else _EPHEMERAL_SECRET

    return hmac.new(
        key,
        email.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _temporarily_unavailable() -> None:
    raise HTTPException(
        status_code=503,
        detail="Authentication is temporarily unavailable.",
    )


def _valid_ip(value: str | None) -> str | None:
    if not isinstance(value, str) or not value:
        return None

    if len(value) > MAX_ADDRESS_LENGTH:
        return None

    try:
        # No comma-separated lists, ports, zone IDs or arbitrary hostnames.
        return ipaddress.ip_address(value).compressed

    except ValueError:
        return None


def resolve_client_ip(request: Request) -> str:
    """Use X-Real-IP only behind an externally verified Railway edge.

    X-Forwarded-For is deliberately ignored: the repository has no verified
    Railway append/overwrite contract for that header.
    """

    if getattr(settings, "production", False):
        if not (
            getattr(settings, "trust_railway_real_ip", False)
            and getattr(settings, "railway_client_ip_verified", False)
        ):
            _temporarily_unavailable()

        # A missing, duplicate or malformed value must never silently fall
        # back to the shared edge proxy address.
        values = request.headers.getlist("x-real-ip")

        if len(values) != 1:
            _temporarily_unavailable()

        resolved = _valid_ip(values[0])

        if resolved is None:
            _temporarily_unavailable()

        return resolved

    # In local/staging mode, forwarding headers are never trusted.
    host = request.client.host if request.client else None
    resolved = _valid_ip(host)

    if resolved is not None:
        return resolved

    # Local ASGI TestClient uses "testclient", not a socket IP.
    if isinstance(host, str) and 0 < len(host) <= MAX_ADDRESS_LENGTH:
        return "local:" + host

    return "unknown"


def _window_for(key: tuple[str, str, str]) -> int:
    scope, action, _ = key

    return (
        ACCOUNT_LIMITS[action][1]
        if scope == "account"
        else LIMITS[action][1]
    )


def _prune(now: float) -> None:
    """Drop expired buckets while the caller holds _lock."""

    for key, timestamps in list(_entries.items()):
        window = _window_for(key)

        while timestamps and now - timestamps[0] >= window:
            timestamps.popleft()

        if not timestamps:
            del _entries[key]


def _too_many(retry_seconds: int) -> None:
    raise HTTPException(
        status_code=429,
        detail="Too many attempts. Try again later.",
        headers={
            "Retry-After": str(max(1, retry_seconds)),
        },
    )


async def check_auth_rate(
    request: Request,
    action: str,
    email: str | None = None,
) -> None:
    """Atomically check/charge IP and optional account request budgets."""

    if action not in LIMITS:
        raise ValueError("Unknown rate-limit action")

    ip = resolve_client_ip(request)
    keys = [("ip", action, ip)]

    if action in ACCOUNT_LIMITS:
        normalized = normalize_account(email)

        if normalized is not None:
            keys.append(("account", action, _account_digest(normalized)))

    now = time.monotonic()

    async with _lock:
        _prune(now)

        # Check all buckets before charging any of them; rejected attempts
        # cannot partially consume the other identity's quota.
        for key in keys:
            timestamps = _entries.get(key)

            if timestamps is None:
                continue

            scope, action_name, _ = key
            limit, window = (
                ACCOUNT_LIMITS[action_name]
                if scope == "account"
                else LIMITS[action_name]
            )

            if len(timestamps) >= limit:
                remaining = window - (now - timestamps[0])
                _too_many(math.ceil(remaining))

        new_buckets = sum(key not in _entries for key in keys)

        if len(_entries) + new_buckets > MAX_IDENTITIES:
            _too_many(max(_window_for(key) for key in keys))

        for key in keys:
            _entries.setdefault(key, deque()).append(now)
