"""FFR-200-5: ReconcileHandler — detect tags that exist but have no release.

Detection only. This module never creates, deletes, or otherwise mutates a tag
or a release. The state ``tag exists, release missing`` is detectable per repo
by checking each tag that exists in the repo against the releases that already
cover it.
"""

import logging
from typing import Iterable

from ops_engine.adapters.base import ForgeAdapter

logger = logging.getLogger(__name__)


class ReconcileHandler:
    """Detects tags that exist but lack a release, without deleting anything."""

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
