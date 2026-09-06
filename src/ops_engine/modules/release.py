"""CORE-002: ReleaseHandler — Create releases from tag pushes or labeled merges."""

import fnmatch
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ops_engine.adapters.base import ForgeAdapter
from ops_engine.adapters.factory import adapters_for
from ops_engine.config_loader import Destination, OpsEngineConfig, ReleaseConfig
from ops_engine.modules.mirror import resolve_destinations
from ops_engine.utils.changelog_parser import ChangelogParser

logger = logging.getLogger(__name__)

# Generic default. It names the repo and the tag, never a product, organisation,
# or host — the product prefix is layover data and reaches the name only through
# a configured ``name_template``.
DEFAULT_NAME_TEMPLATE = "{repo_name} {tag_name}"


class _TemplateDict(dict[str, str]):
    """Mapping that leaves unknown placeholders visible instead of dropping them."""

    def __missing__(self, key: str) -> str:
        return f"{{{key}}}"


def _canonical_slug(repo: str) -> str:
    """Return the repo slug with the org segment lowercased.

    GitHub ``full_name`` carries display case (``LangeVC/ops-engine``) while
    Forgejo always yields the lowercased ``lower_name`` (``langevc/ops-engine``).
    A release name rendered from the slug must not change with the forge that
    delivered the event, so the org segment is normalised to lowercase here —
    the same canonical form the org key already holds (``lower_name``).
    """
    if "/" in repo:
        org, name = repo.split("/", 1)
        return f"{org.lower()}/{name}"
    return repo


def render_release_name(template: str, repo: str, tag_name: str) -> str:
    """Render a release name from a template string.

    Supported placeholders:
      ``{repo}``        full repo slug, forge-independent (``org/repo``,
                        org lowercased to the canonical key)
      ``{repo_name}``   repo short name (``repo``)
      ``{tag_name}``    tag name (``v1.2.3``)

    A single configured ``name_template`` therefore renders the identical name
    whether the event came from Forgejo or from GitHub: the org segment is
    normalised to the canonical lowercase key, and the product prefix (e.g.
    ``"fusionAIze Grid"``) is layover data carried verbatim by the template.

    Unknown placeholders stay as ``{key}`` so a mistyped placeholder is visible
    in the rendered name rather than silently swallowed.
    """
    values = {
        "repo": _canonical_slug(repo),
        "repo_name": repo.split("/")[-1],
        "tag_name": tag_name,
    }
    return template.format_map(_TemplateDict(values))


@dataclass
class PublicationReport:
    """Queryable record of one release published outward to its destinations.

    A release is one build published to every destination resolved from config
    (REL-010). A destination that fails mid-publication is recorded in
    ``failed`` and the remaining destinations still run, so a partial
    publication is a state an operator can read — the mirror is behind — rather
    than a silent success. Serializes to a plain dict via :meth:`to_dict` so a
    cockpit can query it without parsing log lines.
    """

    repo: str = ""
    tag_name: str = ""
    published: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        """True when at least one destination failed to publish."""
        return bool(self.failed)

    def to_dict(self) -> dict[str, object]:
        return {
            "repo": self.repo,
            "tag_name": self.tag_name,
            "published_destinations": list(self.published),
            "failed_destinations": list(self.failed),
            "partial": self.partial,
        }


def _destination_label(destination: Destination) -> str:
    """Name a destination for a report: forge and target, joined.

    Two destinations may carry the same ``repo`` string on different forges, so
    the forge is part of the label; ``github:org/repo`` and ``forgejo:org/repo``
    name two distinct targets.
    """
    return f"{destination.forge}:{destination.repo}"


class ReleaseHandler:
    """Creates releases when tags are pushed or labeled PRs are merged.

    Relation to the release gate: the gate (``.forgejo/workflows/release-gate.yml``
    calling ``scripts/version-sync.py check-tag``) runs in CI at push time, before
    any release object exists, and it runs first. It compares the pushed tag against
    the repository's declared source of truth and turns the run red on any mismatch,
    so a tag whose version lags its content is refused at the gate. This handler is
    the runtime engine that later turns an accepted tag into a release object; it
    does not re-run the gate itself.

    If the gate is absent (no release-gate workflow wired, or the workflow removed),
    nothing in this handler substitutes for it: a mismatched tag would pass through
    and a release object would be created for a version that does not match the
    source of truth. The gate is the only place that refuse happens.
    """

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

    @staticmethod
    async def publish_release(
        config: OpsEngineConfig,
        repo: str,
        tag_name: str,
        release_notes: str,
        artifact_dir: str | Path,
        *,
        token: str = "",
        webhook_secret: str = "",
        base_url: str = "",
        draft: bool = False,
        prerelease: bool = False,
        name_template: str | None = None,
        adapters: list[ForgeAdapter] | None = None,
    ) -> "PublicationReport":
        """Publish a built release to every destination resolved from config.

        The whole job (ADP-003): one build, published outward, nothing
        re-downloaded (REL-010). The destinations come from the config layer via
        :func:`resolve_destinations`; each is turned into an adapter via
        :func:`adapters_for` (ADP-002); for each, the release is created and
        every artifact in ``artifact_dir`` is attached (ADP-001).

        ``artifact_dir`` is the local directory of built artifacts; its files are
        read from disk and attached verbatim. Nothing is downloaded.

        A destination that fails mid-publication is recorded in the returned
        :class:`PublicationReport` under ``failed`` and the remaining
        destinations still run, so a partial publication is a reported state an
        operator can see — the mirror is behind — rather than an exception that
        vanishes or a silent success.

        ``adapters`` is an optional injection point for tests: when omitted, the
        adapters are constructed from the resolved destinations; when supplied,
        they are used in destination order. It never changes the destination
        resolution, which always runs.

        Raises:
            ValueError: a supplied ``adapters`` list does not match the resolved
                destination count.
        """
        destinations = resolve_destinations(config, repo)

        if adapters is None:
            adapters = adapters_for(
                destinations,
                token=token,
                webhook_secret=webhook_secret,
                base_url=base_url,
            )
        if len(adapters) != len(destinations):
            raise ValueError(
                f"publish_release resolved {len(destinations)} destinations but "
                f"received {len(adapters)} adapters"
            )

        release_name = render_release_name(
            name_template or DEFAULT_NAME_TEMPLATE, repo, tag_name
        )
        artifacts = _collect_artifacts(artifact_dir)

        report = PublicationReport(repo=repo, tag_name=tag_name)
        for destination, adapter in zip(destinations, adapters):
            label = _destination_label(destination)
            try:
                await _publish_to_destination(
                    adapter,
                    destination,
                    tag_name=tag_name,
                    release_name=release_name,
                    release_notes=release_notes,
                    artifacts=artifacts,
                    draft=draft,
                    prerelease=prerelease,
                )
            except Exception as exc:  # noqa: BLE001 (record — the report, not abort)
                logger.error(f"partial publication: {label} failed: {exc}")
                report.failed.append(label)
            else:
                report.published.append(label)

        if report.failed:
            logger.error(
                f"release {tag_name} partially published: "
                f"failed destinations {report.failed}"
            )
        return report


async def _publish_to_destination(
    adapter: ForgeAdapter,
    destination: Destination,
    *,
    tag_name: str,
    release_name: str,
    release_notes: str,
    artifacts: list[tuple[str, bytes]],
    draft: bool,
    prerelease: bool,
) -> None:
    """Create the release on one destination and attach every artifact to it.

    The release id comes from ``create_release``'s return value, so the uploads
    target the release object just created — no second lookup and no
    re-download. An exception anywhere in the create-or-attach sequence fails
    the whole destination, which the caller records as a partial publication.
    """
    created = await adapter.create_release(
        repo_full_name=destination.repo,
        tag_name=tag_name,
        name=release_name,
        body=release_notes,
        draft=draft,
        prerelease=prerelease,
    )
    release_id = created["id"]
    for asset_name, data in artifacts:
        await adapter.upload_release_asset(
            repo_full_name=destination.repo,
            release_id=release_id,
            asset_name=asset_name,
            data=data,
        )


def _collect_artifacts(artifact_dir: str | Path) -> list[tuple[str, bytes]]:
    """Read the built artifacts from ``artifact_dir``, name and bytes, sorted.

    Only regular files are attached, by their basename, in name order so a
    destination receives the same attachment sequence every run. The bytes come
    from disk — nothing is downloaded.
    """
    root = Path(artifact_dir)
    artifacts: list[tuple[str, bytes]] = []
    for path in sorted(root.iterdir()):
        if path.is_file():
            artifacts.append((path.name, path.read_bytes()))
    return artifacts


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

    name_template = getattr(config, "name_template", None) or DEFAULT_NAME_TEMPLATE
    release_name = render_release_name(name_template, repo, tag_name)

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
