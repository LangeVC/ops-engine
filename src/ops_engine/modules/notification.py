"""CORE-005: NotificationHandler — Send notifications via webhook, Slack, or Discord."""

import logging
from typing import Any

import httpx

from ops_engine.config_loader import NotificationConfig, NotificationChannel

logger = logging.getLogger(__name__)

# Default templates for different event types
TEMPLATES: dict[str, str] = {
    "release": "**New Release** | `{repo}` {tag_name}\n{body}",
    "merge": "**PR Merged** | `{repo}` #{pr_number} ({merge_method})",
    "mirror_drift": "**Mirror Drift** | `{repo}` primary={primary_sha} mirror={mirror_sha}",
    "default": "**Event** | `{repo}` {event_type}: {action}",
}


class NotificationHandler:
    """Sends notifications to configured channels after events."""

    _seen_events: set[str] = set()

    @classmethod
    async def process_event(
        cls,
        event: dict[str, Any],
        config: NotificationConfig,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Send notifications for an event.

        Args:
            event: Normalized webhook event.
            config: Notification configuration.
            context: Additional context (tag_name, pr_number, etc.)
        """
        if not config.enabled:
            return

        event_type = event.get("event_type", "unknown")
        delivery_id = event.get("delivery_id")

        # Deduplication
        if delivery_id and delivery_id in cls._seen_events:
            return
        if delivery_id:
            cls._seen_events.add(delivery_id)
            # Keep set bounded
            if len(cls._seen_events) > 10000:
                cls._seen_events = set(list(cls._seen_events)[-5000:])

        for channel in config.channels:
            if not _should_notify(channel, event_type):
                continue
            try:
                message = _format_message(event, channel, context or {})
                await _send_notification(channel, message)
            except Exception as e:
                logger.error(f"Failed to send notification to {channel.type}: {e}")

    @classmethod
    def reset(cls) -> None:
        """Reset dedup state (for testing)."""
        cls._seen_events.clear()


def _should_notify(channel: NotificationChannel, event_type: str) -> bool:
    """Check if this channel should receive this event type."""
    if not channel.events:
        return True
    return event_type in channel.events or "*" in channel.events


def _format_message(
    event: dict[str, Any], channel: NotificationChannel, context: dict[str, Any]
) -> str:
    """Format notification message using template."""
    event_type = event.get("event_type", "unknown")
    template_key = channel.template if channel.template != "default" else event_type
    template = TEMPLATES.get(template_key, TEMPLATES["default"])

    values = {
        "repo": event.get("repo", "unknown"),
        "event_type": event_type,
        "action": event.get("action", ""),
        "sender": event.get("sender", ""),
        **context,
    }

    try:
        return template.format_map(type("SafeDict", (dict,), {"__missing__": lambda self, k: f"{{{k}}}"})(**values))
    except Exception:
        return f"Event on {values['repo']}: {event_type} {values.get('action', '')}"


async def _send_notification(channel: NotificationChannel, message: str) -> None:
    """Send notification to a specific channel."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        if channel.type == "slack":
            await client.post(channel.url, json={"text": message})
        elif channel.type == "discord":
            await client.post(channel.url, json={"content": message})
        else:
            # Generic webhook
            await client.post(channel.url, json={"text": message, "message": message})

    logger.info(f"Notification sent to {channel.type}: {message[:80]}...")
