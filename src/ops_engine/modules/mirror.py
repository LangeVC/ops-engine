"""CORE-004: MirrorHandler — Verify mirror sync after push to primary forge."""

import asyncio
import logging
from typing import Any

from ops_engine.adapters.base import ForgeAdapter
from ops_engine.config_loader import MirrorConfig

logger = logging.getLogger(__name__)


class MirrorHandler:
    """Verifies that a mirror repository is in sync after a push to the primary forge."""

    @staticmethod
    async def process_event(
        adapter: ForgeAdapter,
        event: dict[str, Any],
        config: MirrorConfig,
        mirror_adapter: ForgeAdapter | None = None,
    ) -> None:
        """Process a push event and verify mirror sync.

        Args:
            adapter: Adapter for the primary forge (where the push happened).
            event: Normalized webhook event.
            config: Mirror configuration.
            mirror_adapter: Adapter for the mirror forge. If None, uses the same adapter.
        """
        if not config.enabled:
            return

        event_type = event.get("event_type")
        repo = event.get("repo")
        raw = event.get("raw", {})

        if event_type != "push" or not repo:
            return

        # Only verify pushes to default branch (not tags)
        ref = raw.get("ref", "")
        if not ref.startswith("refs/heads/"):
            return
        branch = ref.removeprefix("refs/heads/")

        # Get the SHA that was pushed to primary
        primary_sha = raw.get("after") or raw.get("head_commit", {}).get("id")
        if not primary_sha:
            return

        if not config.mirror_url:
            logger.warning(f"Mirror config for {repo} has no mirror_url")
            return

        # Wait for mirror sync (mirrors are typically async)
        wait_seconds = min(config.max_drift_seconds, 60)
        logger.info(f"Waiting {wait_seconds}s for mirror sync of {repo} -> {config.mirror_url}")
        await asyncio.sleep(wait_seconds)

        # Check mirror SHA
        mirror = mirror_adapter or adapter
        try:
            mirror_sha = await mirror.get_latest_commit_sha(config.mirror_url, branch)
        except Exception as e:
            logger.error(f"Failed to query mirror {config.mirror_url}: {e}")
            await _report_drift(adapter, repo, primary_sha, "unreachable", config)
            return

        if mirror_sha == primary_sha:
            logger.info(f"Mirror {config.mirror_url} is in sync (SHA: {primary_sha[:8]})")
        else:
            logger.warning(
                f"Mirror drift detected: {repo} HEAD={primary_sha[:8]}, "
                f"mirror {config.mirror_url} HEAD={mirror_sha[:8]}"
            )
            await _report_drift(adapter, repo, primary_sha, mirror_sha, config)


async def _report_drift(
    adapter: ForgeAdapter,
    repo: str,
    primary_sha: str,
    mirror_sha: str,
    config: MirrorConfig,
) -> None:
    """Report mirror drift by creating a comment or issue."""
    body = (
        f"**Mirror Drift Detected**\n\n"
        f"- Primary ({repo}): `{primary_sha[:12]}`\n"
        f"- Mirror ({config.mirror_url}): `{mirror_sha[:12] if mirror_sha != 'unreachable' else 'unreachable'}`\n"
        f"- Max allowed drift: {config.max_drift_seconds}s\n\n"
        f"Please verify the mirror sync configuration."
    )
    logger.error(f"Mirror drift: {body}")
    # In a full implementation, this could create an issue or send a notification
