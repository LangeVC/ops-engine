"""FFR-200-5: ReconcileHandler — reconcile tags that exist but have no release.

Detection makes the ``tag exists, release missing`` state visible per repo by
checking each tag that exists against the releases that already cover it. A
reconcile run then creates the release for each missing one, with the name
rendered from the configured ``name_template``, idempotently.
"""

import logging
from typing import Iterable

from ops_engine.adapters.base import ForgeAdapter
from ops_engine.config_loader import ReleaseConfig
from ops_engine.modules.release import DEFAULT_NAME_TEMPLATE, render_release_name

logger = logging.getLogger(__name__)


class ReconcileHandler:
    """Detects and reconciles tags that exist but lack a release."""

    @staticmethod
    async def detect_missing_releases(
        adapter: ForgeAdapter,
        repo: str,
        tag_names: Iterable[str],
    ) -> list[str]:
        """Return the tags from ``tag_names`` that have no release on ``repo``.

        ``tag_names`` are the tags that currently exist in the repo. Each tag is
        checked with ``adapter.release_exists``; the ones without a release are
        returned. The call is read-only: nothing is created, deleted, or
        otherwise mutated, so the ``tag exists, release missing`` state is made
        visible without reconciling it.
        """
        if not repo:
            return []

        missing: list[str] = []
        for tag_name in tag_names:
            if not await adapter.release_exists(repo, tag_name):
                missing.append(tag_name)
        return missing

    @staticmethod
    async def reconcile_releases(
        adapter: ForgeAdapter,
        repo: str,
        tag_names: Iterable[str],
        config: ReleaseConfig,
    ) -> list[str]:
        """Create a release for every tag that exists but lacks one.

        A reconcile run detects the ``tag exists, release missing`` state and
        then creates the missing release for each such tag. The release name is
        rendered from the configured ``name_template`` (falling back to the
        generic default when none is set), so the reconcile path and the normal
        release path name a release identically.

        Idempotent: only tags whose release is absent are created, and the
        existence check is made before every create, so running the reconcile
        again over the same tags creates nothing new.

        Returns the tag names for which a release was created.
        """
        missing = await ReconcileHandler.detect_missing_releases(adapter, repo, tag_names)
        created: list[str] = []
        for tag_name in missing:
            name_template = getattr(config, "name_template", None) or DEFAULT_NAME_TEMPLATE
            release_name = render_release_name(name_template, repo, tag_name)
            await adapter.create_release(
                repo_full_name=repo,
                tag_name=tag_name,
                name=release_name,
                body=f"Release {tag_name}",
                draft=getattr(config, "draft", False),
            )
            created.append(tag_name)
            logger.info(f"Created release {tag_name} on {repo} ({release_name!r})")
        return created
