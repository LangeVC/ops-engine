"""FFR-200-5: ReconcileHandler — reconcile tags that exist but have no release.

Detection makes the ``tag exists, release missing`` state visible per repo by
checking each tag that exists against the releases that already cover it. A
reconcile run then creates the release for each missing one, with the name
rendered from the configured ``name_template``, idempotently.

Release and mirror state are queryable as records, not only visible in a log:
:class:`ReconcileRecord` and :class:`MirrorState` serialize to plain dicts via
``to_dict`` so a cockpit can read them without parsing log lines.
"""

import logging
from dataclasses import dataclass, field
from typing import Iterable

from ops_engine.adapters.base import ForgeAdapter
from ops_engine.config_loader import ReleaseConfig
from ops_engine.modules.release import DEFAULT_NAME_TEMPLATE, render_release_name

logger = logging.getLogger(__name__)


@dataclass
class MirrorState:
    """Queryable snapshot of one repo's cross-forge mirror sync.

    A mirror's state is normally only written to a log line by the mirror
    handler. This record captures the primary and mirror SHAs plus whether they
    agree, so the state can be queried instead of grep'd.
    """

    repo: str = ""
    primary_sha: str = ""
    mirror_sha: str = ""
    in_sync: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "repo": self.repo,
            "primary_sha": self.primary_sha,
            "mirror_sha": self.mirror_sha,
            "in_sync": self.in_sync,
        }


@dataclass
class ReconcileRecord:
    """Queryable record of a release reconcile over one repo.

    Carries the tags that exist without a release and the ones a reconcile run
    actually created, plus the mirror state under the same repo. Serializes to a
    plain dict so a cockpit can query it rather than reading log output.
    """

    repo: str = ""
    missing: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    mirror: MirrorState = field(default_factory=MirrorState)

    def to_dict(self) -> dict[str, object]:
        return {
            "repo": self.repo,
            "missing_releases": list(self.missing),
            "created_releases": list(self.created),
            "mirror": self.mirror.to_dict(),
        }


class ReconcileHandler:
    """Detects and reconciles tags that exist but lack a release, and exposes
    the release and mirror state as queryable records."""
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

    @staticmethod
    async def query_mirror_state(
        adapter: ForgeAdapter,
        repo: str,
        mirror_url: str,
        branch: str = "main",
        primary_sha: str = "",
    ) -> MirrorState:
        """Query the mirror sync state of ``repo`` as a record.

        Fetches the primary SHA (when not supplied) and the mirror's latest
        commit SHA, then returns a :class:`MirrorState` describing whether they
        agree. The query never writes; it only reads both forges, so the mirror
        state is queryable without mutating anything.
        """
        if not repo or not mirror_url:
            return MirrorState(repo=repo, primary_sha=primary_sha)

        if not primary_sha:
            try:
                primary_sha = await adapter.get_latest_commit_sha(repo, branch)
            except Exception as e:  # noqa: BLE001 (broad — record, not abort)
                logger.error(f"Failed to query primary {repo}: {e}")

        try:
            mirror_sha = await adapter.get_latest_commit_sha(mirror_url, branch)
        except Exception as e:  # noqa: BLE001 (broad — record, not abort)
            logger.error(f"Failed to query mirror {mirror_url}: {e}")
            return MirrorState(
                repo=repo,
                primary_sha=primary_sha,
                mirror_sha="",
                in_sync=False,
            )

        return MirrorState(
            repo=repo,
            primary_sha=primary_sha,
            mirror_sha=mirror_sha,
            in_sync=bool(primary_sha) and mirror_sha == primary_sha,
        )

    @staticmethod
    async def query_state(
        adapter: ForgeAdapter,
        repo: str,
        tag_names: Iterable[str],
        *,
        mirror_url: str = "",
        branch: str = "main",
        primary_sha: str = "",
    ) -> ReconcileRecord:
        """Return the release and mirror state of ``repo`` as a single record.

        Read-only: it detects the tags that exist without a release and queries
        the mirror sync state, then returns a :class:`ReconcileRecord` that
        serializes both to a plain dict. A cockpit can query this record instead
        of reading log output.
        """
        missing = await ReconcileHandler.detect_missing_releases(adapter, repo, tag_names)
        mirror = await ReconcileHandler.query_mirror_state(
            adapter, repo, mirror_url, branch=branch, primary_sha=primary_sha
        )
        return ReconcileRecord(repo=repo, missing=missing, mirror=mirror)
