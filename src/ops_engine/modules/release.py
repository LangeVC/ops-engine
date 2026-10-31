"""CORE-002: ReleaseHandler — Create releases from tag pushes or labeled merges."""

import fnmatch
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from ops_engine.adapters.base import ForgeAdapter
from ops_engine.adapters.factory import adapters_for
from ops_engine.config_loader import Destination, OpsEngineConfig, ReleaseConfig
from ops_engine.modules.mirror import resolve_destinations
from ops_engine.modules.notification import NotificationHandler
from ops_engine.modules.release_gates import (
    MissingChangelogEntryError,
    check_release_notes_audience,
    check_release_title,
    extract_changelog_section,
)
from ops_engine.modules.tag_identity import Runner, push_canonical_tag

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
    skipped: list[str] = field(default_factory=list)

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


class ArtifactDirError(ValueError):
    """``release.artifact_dir`` is absolute or leaves the artifact root.

    Raised before any write, so nothing is attached and no tag is pushed.
    """


class MissingReleaseContextError(RuntimeError):
    """A tag push reached the release path with no way to publish it.

    The webhook path no longer creates a bare release on the event's own forge.
    Without a :class:`ReleaseContext` there are no gates' inputs, no identity
    and no destinations, so the push is refused before any release object
    exists.
    """


class MissingOpsYamlError(RuntimeError):
    """The tag carries no readable ``.ops.yaml``: no destinations can be named."""


class MissingReleaseDestinationsError(RuntimeError):
    """``.ops.yaml`` at the tag declares no destination with the release role."""


@dataclass
class TagIdentitySpec:
    """Inputs of the OE-008 tag identity step (:func:`push_canonical_tag`).

    ``canonical_token`` reaches git through the environment, never argv or a
    URL; ``mirror_remote`` is used exactly as given. ``runner`` is the git
    runner injection point (real git when ``None``).
    """

    repo_dir: str
    canonical_url: str
    canonical_token: str
    mirror_remote: str
    runner: Optional[Runner] = None


@dataclass
class ReleaseContext:
    """Everything a tag-push release needs besides the event itself.

    ``ops_config`` is the Layer-2 config the destinations resolve from; the
    Layer-3 ``.ops.yaml`` is read from the pushed tag and merged over it.
    ``ticket_prefixes`` is the organisation's tracker vocabulary for the
    audience gate (empty: the gate refuses nothing). ``adapters`` is the test
    injection point of :meth:`ReleaseHandler.publish_release`.
    """

    ops_config: OpsEngineConfig
    artifact_dir: str | Path
    identity: TagIdentitySpec
    token: str = ""
    webhook_secret: str = ""
    base_url: str = ""
    name_template: Optional[str] = None
    ticket_prefixes: Iterable[str] = ()
    prerelease: bool = False
    adapters: Optional[list[ForgeAdapter]] = None
    audit: Optional[list[str]] = None


def _resolve_artifact_dir(root: str | Path, configured: Optional[str]) -> Path:
    """Resolve ``release.artifact_dir`` under the artifact ``root``, or refuse.

    ``configured`` comes from the layover's own :class:`ReleaseConfig`; ``None``
    means the root itself. The result must stay inside ``root`` once symlinks
    and ``..`` are resolved. A directory that does not exist is fine: it
    attaches nothing.
    """
    if not configured:
        return Path(root)
    if Path(configured).is_absolute() or ".." in Path(configured).parts:
        raise ArtifactDirError(
            f"ArtifactDirError: release.artifact_dir {configured!r} must be a "
            "relative path inside the artifact root; nothing attached."
        )
    resolved_root = Path(root).resolve()
    candidate = (resolved_root / configured).resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise ArtifactDirError(
            f"ArtifactDirError: release.artifact_dir {configured!r} resolves "
            "outside the artifact root; nothing attached."
        )
    return candidate


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
        adapter: ForgeAdapter,
        event: dict[str, Any],
        config: ReleaseConfig,
        context: Optional[ReleaseContext] = None,
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
                if context is None:
                    raise MissingReleaseContextError(
                        f"MissingReleaseContextError: tag {tag_name} pushed on "
                        f"{repo} but no ReleaseContext was supplied; refusing "
                        "before any release object exists."
                    )
                await _release_tag(adapter, repo, tag_name, config, context)
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
        repo_dir: str | Path | None = None,
        roles: Iterable[str] | None = None,
        skip_existing: bool = False,
        audit: Optional[list[str]] = None,
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

        ``repo_dir`` is a directory holding the Layer-3 ``.ops.yaml`` to merge
        over the Layer-2 config (the webhook path writes the file it read at
        the tag there). ``roles`` keeps only destinations whose role is listed;
        ``None`` keeps all.

        ``skip_existing`` makes publication idempotent per destination: when the
        destination already holds a release for ``tag_name`` it is recorded in
        ``skipped`` and nothing is created or uploaded there, so a redelivered
        event is a no-op for it. The check runs on the destination's own
        adapter (each destination answers for itself). It is opt-in so callers
        that publish to a known-empty destination keep their exact call
        sequence.

        ``audit`` follows the ``MirrorHandler.push_event`` convention: when a
        list is given, one line per destination is appended to it
        (``release-publish tag=... dest=... result=published|failed|skipped``).
        A destination skipped because its release already exists is not a
        failure. When any destination failed, one warning line is logged for
        the whole call (the engine has no event log, D20).

        Raises:
            ValueError: a supplied ``adapters`` list does not match the resolved
                destination count.
        """
        destinations = _resolve_publish_destinations(config, repo, repo_dir, roles)

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
                if skip_existing and await adapter.release_exists(
                    destination.repo, tag_name
                ):
                    logger.info(f"Release {tag_name} already exists on {label}, skipping")
                    report.skipped.append(label)
                    _audit_destination(audit, tag_name, label, "skipped")
                    continue
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
                _audit_destination(audit, tag_name, label, "failed")
            else:
                report.published.append(label)
                _audit_destination(audit, tag_name, label, "published")

        if report.partial:
            logger.warning(
                f"release-partial tag={tag_name} repo={repo} "
                f"published={report.published} failed={report.failed}"
            )
        return report


def _audit_destination(
    audit: Optional[list[str]], tag_name: str, label: str, result: str
) -> None:
    if audit is not None:
        audit.append(f"release-publish tag={tag_name} dest={label} result={result}")


async def _notify_partial(
    ops_config: OpsEngineConfig, repo: str, tag_name: str, report: PublicationReport
) -> None:
    """Report a partial publication through the notification channel.

    The channels come from the Layer-2 config only (a repository's ``.ops.yaml``
    at the tag cannot add a channel URL). No channels configured: nothing is
    sent. The event carries no delivery id, so a retry is never deduplicated
    away.
    """
    org, _, name = repo.partition("/")
    notifications = ops_config.get_repo_config(org, name).notifications
    if notifications is None:
        return
    await NotificationHandler.process_event(
        {"event_type": "release", "repo": repo, "action": "partial_failure"},
        notifications,
        {
            "tag_name": tag_name,
            "body": (
                "FAILED: partial publication. "
                f"published={report.published} failed={report.failed}"
            ),
        },
    )


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
    if not root.is_dir():
        return artifacts
    for path in sorted(root.iterdir()):
        if path.is_file():
            artifacts.append((path.name, path.read_bytes()))
    return artifacts


def _resolve_publish_destinations(
    config: OpsEngineConfig,
    repo: str,
    repo_dir: str | Path | None,
    roles: Iterable[str] | None,
) -> list[Destination]:
    destinations = resolve_destinations(
        config, repo, repo_dir=str(repo_dir) if repo_dir is not None else None
    )
    if roles is None:
        return destinations
    wanted = set(roles)
    return [d for d in destinations if d.role in wanted]


async def _release_tag(
    adapter: ForgeAdapter,
    repo: str,
    tag_name: str,
    config: ReleaseConfig,
    context: ReleaseContext,
) -> None:
    """Release a pushed tag: all gates, then the tag identity, then publication.

    Everything read here is read AT the tag. The first three steps only read and
    refuse; the identity step is the first to write (a tag on the mirror) and the
    publication the second, so any refusal in the gates or in the destination
    resolution happens before a tag is pushed or a release object exists.
    """
    # Artifact directory from the layover's own config (D18); refused before
    # any read or write if it leaves the artifact root.
    artifact_dir = _resolve_artifact_dir(
        context.artifact_dir, getattr(config, "artifact_dir", None)
    )

    # Gates (OE-007) on the CHANGELOG and the tag.
    name_template = (
        context.name_template
        or getattr(config, "name_template", None)
        or DEFAULT_NAME_TEMPLATE
    )
    title_form = render_release_name(name_template, repo, "{tag_name}")
    check_release_title(tag_name, name_template=title_form)

    try:
        changelog = await adapter.get_file_content(
            repo, config.changelog_path, ref=tag_name
        )
    except Exception as exc:  # noqa: BLE001 (a named refusal, not a fallback)
        raise MissingChangelogEntryError(
            f"MissingChangelogEntryError: could not read {config.changelog_path} "
            f"of {repo} at {tag_name}: {exc}. No notes, so no release."
        ) from exc
    notes = extract_changelog_section(changelog, tag_name)
    check_release_notes_audience(notes, context.ticket_prefixes)

    # Destinations from .ops.yaml at the tag; read-only, still before any write.
    try:
        ops_yaml = await adapter.get_file_content(repo, ".ops.yaml", ref=tag_name)
    except Exception as exc:  # noqa: BLE001
        raise MissingOpsYamlError(
            f"MissingOpsYamlError: no readable .ops.yaml of {repo} at {tag_name}: "
            f"{exc}. Refusing rather than publishing to fewer destinations."
        ) from exc

    with tempfile.TemporaryDirectory() as layer3_dir:
        (Path(layer3_dir) / ".ops.yaml").write_text(ops_yaml, encoding="utf-8")
        if not _resolve_publish_destinations(
            context.ops_config, repo, layer3_dir, ("release",)
        ):
            raise MissingReleaseDestinationsError(
                f"MissingReleaseDestinationsError: .ops.yaml of {repo} at "
                f"{tag_name} declares no destination with role 'release'."
            )

        # Identity (OE-008), after every gate.
        identity = context.identity
        await push_canonical_tag(
            identity.repo_dir,
            tag_name,
            identity.canonical_url,
            identity.canonical_token,
            identity.mirror_remote,
            identity.runner,
        )

        logger.info(f"Publishing release {tag_name} of {repo}")
        report = await ReleaseHandler.publish_release(
            context.ops_config,
            repo,
            tag_name,
            notes,
            artifact_dir,
            token=context.token,
            webhook_secret=context.webhook_secret,
            base_url=context.base_url,
            draft=config.draft,
            prerelease=context.prerelease,
            name_template=name_template,
            adapters=context.adapters,
            repo_dir=layer3_dir,
            roles=("release",),
            skip_existing=True,
            audit=context.audit,
        )
    if report.partial:
        await _notify_partial(context.ops_config, repo, tag_name, report)


def _extract_tag_name(event_type: str, raw: dict[str, Any]) -> str | None:
    """Extract tag name from push or create events."""
    if event_type == "create" and raw.get("ref_type") == "tag":
        return raw.get("ref")

    if event_type == "push":
        ref = raw.get("ref", "")
        if ref.startswith("refs/tags/"):
            return ref.removeprefix("refs/tags/")

    return None
