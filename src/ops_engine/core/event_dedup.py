"""CORE-008: Event Deduplication — Prevent duplicate actions from webhook retries."""

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class EventDeduplicator:
    """In-memory event deduplication using delivery IDs with configurable TTL.

    Tracks event delivery IDs (X-GitHub-Delivery, X-Forgejo-Delivery)
    to prevent duplicate processing when webhooks are retried.
    """

    def __init__(self, ttl_seconds: int = 3600):
        """Initialize deduplicator.

        Args:
            ttl_seconds: Time-to-live for seen event IDs (default: 1 hour).
        """
        self.ttl_seconds = ttl_seconds
        self._seen: dict[str, float] = {}

    def is_duplicate(self, delivery_id: Optional[str]) -> bool:
        """Check if an event delivery ID has been seen before.

        Args:
            delivery_id: The webhook delivery ID from headers.

        Returns:
            True if this delivery ID was already processed.
        """
        if not delivery_id:
            return False

        self._evict_expired()

        if delivery_id in self._seen:
            logger.info(f"Duplicate event detected: {delivery_id}")
            return True

        self._seen[delivery_id] = time.monotonic()
        return False

    def extract_delivery_id(self, headers: dict[str, str]) -> Optional[str]:
        """Extract delivery ID from webhook headers.

        Supports both GitHub and Forgejo/Gitea header formats.
        """
        return (
            headers.get("x-github-delivery")
            or headers.get("x-forgejo-delivery")
            or headers.get("x-gitea-delivery")
        )

    def _evict_expired(self) -> None:
        """Remove entries older than TTL."""
        now = time.monotonic()
        cutoff = now - self.ttl_seconds
        expired = [k for k, ts in self._seen.items() if ts < cutoff]
        for k in expired:
            del self._seen[k]

    @property
    def size(self) -> int:
        """Number of tracked delivery IDs."""
        return len(self._seen)

    def clear(self) -> None:
        """Clear all tracked delivery IDs."""
        self._seen.clear()
