"""Rate-limit mixin for Mortify HTTP views.

Mirrors Beatify's ``RateLimitMixin`` so every game/admin endpoint gets
IP-based DoS protection, not just the QR and WebSocket handlers.
"""

from __future__ import annotations

import logging
import time

_LOGGER = logging.getLogger(__name__)


class RateLimitMixin:
    """Mixin providing IP-based rate limiting for HTTP views."""

    RATE_LIMIT_REQUESTS: int = 5
    RATE_LIMIT_WINDOW: int = 60  # seconds

    def _init_rate_limits(self) -> None:
        """Initialise rate-limit state. Call from ``__init__``."""
        self._rate_limits: dict[str, list[float]] = {}
        self._last_rate_sweep: float = 0.0

    def _check_rate_limit(self, ip: str) -> bool:
        """Return False if *ip* has exceeded the rate limit."""
        now = time.time()
        cutoff = now - self.RATE_LIMIT_WINDOW
        if now - self._last_rate_sweep > 300:
            self._rate_limits = {
                k: [t for t in v if t > cutoff]
                for k, v in self._rate_limits.items()
                if any(t > cutoff for t in v)
            }
            self._last_rate_sweep = now
        times = [t for t in self._rate_limits.get(ip, []) if t > cutoff]
        self._rate_limits[ip] = times
        if len(times) >= self.RATE_LIMIT_REQUESTS:
            return False
        times.append(now)
        return True
