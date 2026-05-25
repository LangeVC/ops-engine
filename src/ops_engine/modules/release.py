"""CORE-002: ReleaseHandler — Create releases from tag pushes or labeled merges."""

import fnmatch
import logging
from typing import Any

from ops_engine.adapters.base import ForgeAdapter
from ops_engine.config_loader import ReleaseConfig
from ops_engine.utils.changelog_parser import ChangelogParser

logger = logging.getLogger(__name__)


class ReleaseHandler:
    """Creates releases when tags are pushed or labeled PRs are merged."""

    @staticmethod
    async def process_event(
        adapter: ForgeAdapter, event: dict[str, Any], config: ReleaseConfig
    ) -> None:
        if not config.enabled:
            return

        event_type = event.get("event_type")
        action = event.get("action")
        repo = event.get("repo")
        raw = event.get("raw", {})

        if not repo:
            return

        # Trigger 1: Tag push
        if event_type in ("push", "create") and config.trigger in ("tag_push", "both"):
            tag_name = _extract_tag_name(event_type, raw)
            if tag_name and fnmatch.fnmatch(tag_name, config.tag_pattern):
                await _create_release_for_tag(adapter, repo, tag_name, config)
                return

        # Trigger 2: PR merged with label
        if (
            event_type == "pull_request"
            and action == "closed"
            and raw.get("pull_request", {}).get("merged")
            and config.trigger in ("merge_label", "both")
        ):
            if config.create_tag_on_merge:
                # Try to detect version bump and auto-tag
                logger.info(f"PR merged on {repo}, create_tag_on_merge is enabled (not yet implemented)")


async def _create_release_for_tag(
    adapter: ForgeAdapter, repo: str, tag_name: str, config: ReleaseConfig
) -> None:
    """Create a release for a given tag, with CHANGELOG-based notes."""

    # Idempotency: check if release already exists
    if await adapter.release_exists(repo, tag_name):
        logger.info(f"Release {tag_name} already exists on {repo}, skipping")
        return

    # Try to extract release notes from CHANGELOG
    release_notes = ""
    if config.changelog_path:
        try:
            content = await adapter.get_file_content(repo, config.changelog_path, ref=tag_name)
            version = tag_name.lstrip("v")
            release_notes = ChangelogParser.extract_from_file(content, version)
        except Exception as e:
            logger.warning(f"Could not read {config.changelog_path} from {repo}: {e}")

    if not release_notes:
        release_notes = f"Release {tag_name}"

    release_name = f"{repo.split('/')[-1]} {tag_name}"

    logger.info(f"Creating release {tag_name} on {repo}")
    await adapter.create_release(
        repo_full_name=repo,
        tag_name=tag_name,
        name=release_name,
        body=release_notes,
        draft=config.draft,
    )
    logger.info(f"Release {tag_name} created on {repo}")


def _extract_tag_name(event_type: str, raw: dict[str, Any]) -> str | None:
    """Extract tag name from push or create events."""
    if event_type == "create" and raw.get("ref_type") == "tag":
        return raw.get("ref")

    if event_type == "push":
        ref = raw.get("ref", "")
        if ref.startswith("refs/tags/"):
            return ref.removeprefix("refs/tags/")

    return None
