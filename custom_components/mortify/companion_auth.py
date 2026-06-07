"""HA Android Companion auth-bypass helpers for Mortify.

Adopted from Beatify's ``companion_auth.py``. The HA Android Companion App
ships a WebView that intercepts OAuth redirects to ``/auth/authorize``, so
the standard ha-auth.js token bootstrap dies on "Invalid redirect URI" before
any Mortify endpoint sees a Bearer token.

This module provides a narrowly-scoped bypass: when a request bears all
indicators of an HA Android Companion WebView on the local network, Mortify
treats it as authenticated even without a Bearer token. The bypass is
intentionally conservative — desktop browsers, iOS Companion, and any
internet-origin request fall through to the unchanged OAuth path.

Threat model: the bypass adds no new attack surface beyond "anyone on the
local network who can spoof an HA-Android-Companion User-Agent gains the
admin endpoints". On a residential LAN this is roughly equivalent to "the
network is already trusted".
"""

from __future__ import annotations

import ipaddress
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiohttp import web
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_ANDROID_RE = re.compile(r"Android", re.IGNORECASE)
_HA_APP_RE = re.compile(r"Home\s?Assistant|HACompanion|Hass", re.IGNORECASE)

_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("::1/128"),
]


def is_local_remote(remote: str | None) -> bool:
    """Return True if ``remote`` is an RFC1918 / loopback / ULA address."""
    if not isinstance(remote, str) or not remote:
        return False
    try:
        ip = ipaddress.ip_address(remote)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in net for net in _PRIVATE_NETS)


def is_companion_ua(user_agent: str | None) -> bool:
    """Return True if ``user_agent`` looks like HA's Android Companion App."""
    if not isinstance(user_agent, str) or not user_agent:
        return False
    return bool(_ANDROID_RE.search(user_agent)) and bool(_HA_APP_RE.search(user_agent))


def is_companion_trusted_request(request: web.Request) -> bool:
    """Return True if the HTTP request comes from a trusted HA Android Companion.

    Trust requires ALL of:
      1. User-Agent matches the HA Android Companion regex.
      2. Source IP is private/loopback.
    """
    ua = request.headers.get("User-Agent")
    remote = request.remote
    return is_companion_ua(ua) and is_local_remote(remote)


def is_companion_trusted_meta(meta: dict | None) -> bool:
    """Same check as ``is_companion_trusted_request`` but for WebSocket meta."""
    if not isinstance(meta, dict):
        return False
    ua = meta.get("ua")
    remote = meta.get("remote")
    return is_companion_ua(ua) and is_local_remote(remote)


async def is_authorized_http(request: web.Request, hass: HomeAssistant) -> bool:
    """Return True if the request may invoke a protected endpoint.

    Two paths accepted:
      - Standard HA Bearer token (validated via hass.auth).
      - Companion bypass (see ``is_companion_trusted_request``).

    Views that previously relied on ``requires_auth = True`` can flip to
    ``requires_auth = False`` and call this helper at the top of their handler.
    """
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token:
            result = hass.auth.async_validate_access_token(token)
            if result is not None:
                return True

    return is_companion_trusted_request(request)


def extract_request_meta(request: web.Request) -> dict:
    """Pull User-Agent and remote from an HTTP request for WS trust evaluation."""
    return {
        "ua": request.headers.get("User-Agent"),
        "remote": request.remote,
    }
