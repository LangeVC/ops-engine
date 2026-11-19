"""CORE-002: ReleaseHandler — Create releases from tag pushes or labeled merges."""

import fnmatch
import hashlib
import logging
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx

from ops_engine.adapters.base import ForgeAdapter
from ops_engine.adapters.factory import Credential, MissingCredentialError, adapters_for
from ops_engine.config_loader import (
    AssetManifest,
    Destination,
    OpsEngineConfig,
    ReleaseConfig,
    load_ops_yaml,
)
from ops_engine.modules.asset_manifest import (
    HttpxChannelReader,
    deadline_passed,
    evaluate_channels,
    parse_timestamp,
)
from ops_engine.modules.mirror import resolve_destinations
from ops_engine.modules.notification import NotificationHandler
from ops_engine.modules.release_gates import (
    MissingChangelogEntryError,
    ReleaseNotesAudienceError,
    ReleaseTitleError,
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

# P36-017 criterion 5: RC tag guard. A tag matching vX.Y.Z-rc.N (the form
# _TAG_RE in release_gates.py already accepts) is not created, written or
# mirrored on any public destination; the destination is skipped with one
# named log line and the run summary lists the reason.
_RC_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+-rc\.?\d+$")


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
    # Destinations whose existing release was converged to the intended state
    # (D1): title, body or missing files written. An object authored by
    # another account is converged the same way. Not failures: they are not in
    # ``failed``.
    supplemented: list[str] = field(default_factory=list)
    # label -> what an operator needs to read: the HTTP status and the
    # destination's existing release id (409/422), or what differs on an
    # existing release (incomplete). Both labels are also in ``failed``.
    failure_details: dict[str, str] = field(default_factory=dict)
    # Destinations whose existing release for the tag already is the intended
    # state, so nothing was written: recorded as up to date, never edited and
    # never duplicated. A release from ANOTHER publisher is converged like any
    # other (D1, superseding P36-003); only a complete one lands here.
    # Not failures: they are not in ``failed``.
    up_to_date: list[str] = field(default_factory=list)
    # destination label -> who made the release found for the tag there:
    # ``engine`` (the account named by ``publisher_login``) or ``other``.
    publishers: dict[str, str] = field(default_factory=dict)
    # P36-017 criterion 5: destinations skipped because the tag is an RC
    # tag that must not reach a public destination.
    rc_skipped: list[str] = field(default_factory=list)
    # P36-018 criterion 1: body state per destination (written, supplemented,
    # already_equal, or failed with the named difference).
    body_states: dict[str, str] = field(default_factory=dict)
    # REA-014: destination label -> the declared release-object files found on
    # read-back, each as ``"<name> sha256:<hex>"`` of the build's own bytes.
    # Empty when the repo declares no release-object asset.
    verified: dict[str, list[str]] = field(default_factory=dict)
    # REA-016: destination label -> the manifest pieces still on their way,
    # each as ``"<name> producer=<forge> since=<iso>"``. A pending piece is
    # not a failure: it is a github-produced asset inside the deadline.
    pending: dict[str, list[str]] = field(default_factory=dict)

    @property
    def publisher(self) -> dict[str, str]:
        """Who made the release found for this tag, per destination.

        Returns the ``publishers`` dict mapping destination labels to
        ``engine`` or ``other``. An empty dict means no attribution was
        possible (no publisher_login supplied, or no release existed).
        """
        return dict(self.publishers)

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
            "up_to_date_destinations": list(self.up_to_date),
            "publishers": dict(self.publishers),
            "partial": self.partial,
            "rc_skipped_destinations": list(self.rc_skipped),
            "body_states": dict(self.body_states),
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
    URL; ``mirror_remote`` is used token-free; ``mirror_token``, when given,
    is passed through the environment, never argv or URL (P36-002). ``runner``
    is the git runner injection point (real git when ``None``).
    """

    repo_dir: str
    canonical_url: str
    canonical_token: str
    mirror_remote: str
    runner: Optional[Runner] = None
    mirror_token: str = ""


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
    credentials: Optional[dict[str, Credential]] = None
    audit: Optional[list[str]] = None
    publisher_login: str | dict[str, str] = ""
    # REA-016: read-only channel probe, the same injection point as
    # ``publish_release``'s. Omitted, the shipped HttpxChannelReader runs.
    channel_reader: object = None
    # P36-017: per-repository release body parts. The install block and assets
    # line come from a per-repository declaration (business data, for example a
    # key in the repository's .ops.yaml), never from engine defaults.
    release_body_install_block: str = ""
    release_body_assets_line: str = ""


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
        credentials: dict[str, Credential] | None = None,
        publisher_login: str | dict[str, str] = "",
        release_body: str | None = None,
        ticket_prefixes: Iterable[str] | None = None,
        channel_reader: object = None,
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

        ``skip_existing`` adds a pre-flight check that makes publication
        idempotent per destination: when the destination already holds a release
        for ``tag_name`` it is recorded in ``skipped`` and nothing is created or
        uploaded there, so a redelivered event is a no-op for it. The check runs
        on the destination's own adapter (each destination answers for itself).

        A duplicate answer to the create call is continued from whether or not
        ``skip_existing`` was given (REA-011): 409 on Forgejo, 422 on GitHub,
        which mean the destination already holds a release for the tag in some
        state — a draft, or a row the token's list does not show. The existing
        release is resolved by tag for any state and judged like one the
        pre-flight check found; the audit line of that judgement names the
        duplicate answer (``http=<status> existing_release_id=<id>``), so this
        is never a failure.

        ``audit`` follows the ``MirrorHandler.push_event`` convention: when a
        list is given, one line per destination is appended to it
        (``release-publish tag=... dest=... result=published|failed|skipped|incomplete``).
        A destination skipped because its release already exists is not a
        failure, unless that release is incomplete (its title is not the gated
        title, or it lacks assets of this build): it is then recorded in
        ``failed`` and ``incomplete`` names what differs. A 422 that is not a
        duplicate answer (GitHub's generic validation failure, a code other
        than ``already_exists``) is a failure and carries ``http=<status>
        existing_release_id=<id>``, in the audit line and in the notification.

        An existing release is converged to the intended state (REA-012,
        decision D1, which supersedes C1's "existing object only recorded"):
        for the declared tag the title is set per the name template and the
        body to the CHANGELOG body, and the object is never deleted or
        re-created. An object with another title is PATCHed to the template
        title; an empty body is filled with the CHANGELOG body; the audit line
        names the old and the new title and lists the files attached. When the
        title is equal ignoring case and whitespace, the notes of this build
        (equal after stripping) are in place and only files are missing, it is
        supplemented: the title is corrected and exactly the missing files are
        uploaded. It is then listed in ``supplemented`` and the audit line
        reads ``result=supplemented``. Files already attached are untouched;
        nothing is re-created or deleted. When any destination failed, one
        warning line is logged for the whole call (the engine has no event
        log, D20).

        ``release_body`` is the composed body (title, notes, install and
        CHANGELOG link). With ``ticket_prefixes`` given, the external-audience
        gate runs on that final body per destination before any write: a body
        that names the author's tracker writes nothing on that destination and
        fails it by name, while a destination with a clean body still
        publishes. ``process_event`` keeps raising the refusal from its own
        gate before any destination is considered; this path records it.

        ``publisher_login`` is the login the engine publishes as (P36-003). A
        repository can have two publishers — the engine and a repository-local
        workflow — so the release object's own ``author.login`` is read and
        the report's ``publisher`` says ``engine`` or ``other``. Attribution
        does NOT exempt an object from judgement: decision D1 supersedes
        P36-003's "recorded only", so a release authored by another account is
        converged to the intended state (title per the template, the CHANGELOG
        body, the missing files) exactly like one without an author; it is
        edited in place, never deleted or duplicated. A foreign release that
        already IS the intended state writes nothing and is recorded in
        ``up_to_date``. Without the parameter the engine cannot
        attribute a release and every release found is judged as before. An
        undeterminable author degrades to ``other``, never to a false
        ``engine``.

        A ``dict[str, str]`` maps credential-variable names to logins (one
        login per destination, P36-003 design C1). Any dict (including an
        empty one) means per-destination mode: a destination whose
        credential_variable is absent or maps to an empty string means the
        derivation failed for that destination and it is failed by name;
        nothing is supplemented, edited or duplicated (fail closed).

        ``credentials`` is a per-destination credential mapping (ADP-011):
        each destination's forge value maps to its own :class:`Credential`,
        so a Forgejo destination never receives the GitHub token and vice
        versa (P36-002). Omitted or ``None`` falls back to the legacy shared
        ``token``/``webhook_secret`` pair.

        ``channel_reader`` is an optional injection point for tests: an object
        with ``async present_at(channel, name, version)``. Omitted, the shipped
        read-only :class:`HttpxChannelReader` probes PyPI, ghcr and the
        Homebrew tap with ``GET`` only.

        Raises:
            ValueError: a supplied ``adapters`` list does not match the resolved
                destination count.
        """
        destinations = _resolve_publish_destinations(config, repo, repo_dir, roles)

        release_name = render_release_name(
            name_template or DEFAULT_NAME_TEMPLATE, repo, tag_name
        )
        artifacts = _collect_artifacts(artifact_dir)
        # REA-014: the release-object files this repo declares, resolved from
        # the same Layer 2 + ``.ops.yaml`` merge the destinations come from.
        # Empty: nothing to verify, and the destination keeps its historic
        # single audit line.
        declared = _declared_release_asset_names(config, repo, repo_dir, tag_name)
        # REA-016: the manifest's pieces per destination label. None means the
        # repo declares no manifest, so every asset is forgejo-produced and
        # required at once (the REA-014 behaviour, unchanged).
        manifest = _resolved_manifest(config, repo, repo_dir)
        requirements = (
            _manifest_requirements(manifest, repo, tag_name)
            if manifest is not None else {}
        )
        now = datetime.now(timezone.utc)

        report = PublicationReport(repo=repo, tag_name=tag_name)
        # Destinations this run read back, and the release clock the pending
        # deadline runs from: the first release object's own created_at.
        seen: set[str] = set()
        observed_since: list[Optional[datetime]] = [None]
        # The clock the pending deadline runs from is the release object's own
        # created_at. A manifest destination this run does not visit (a
        # github-built piece on a github destination another workflow owns)
        # has no object to read here: without a clock its pieces stay pending,
        # never red and never on another destination's clock.

        async def _verify(adapter: ForgeAdapter, destination: Destination,
                          label: str) -> bool:
            """Check a destination after publication; True if it audited itself.

            Without a manifest this is the REA-014 read-back: every declared
            file must be attached, and a failure is ``verify-failed``. With a
            manifest each piece carries a producer: a github-produced piece
            still on its way is pending inside the deadline and red after it,
            while a forgejo-produced piece is required at once (D4/D5).
            """
            # A destination the judgement just failed (an incomplete object) is
            # not verified — its outcome already names the difference, and a
            # second line saying the set looked right would contradict it.
            if label in report.failed:
                return True
            # No manifest: the REA-014 read-back, unchanged. With nothing
            # declared either there is nothing to check and the destination
            # keeps its historic single ``published`` line.
            if manifest is None:
                if not declared:
                    return False
                await _verify_destination_assets(
                    adapter, destination, report, audit, tag_name, label,
                    declared, artifacts,
                )
                return True
            # A manifest that names no piece for this destination leaves it
            # alone (D4: a GitHub-built binary stays on GitHub). A declared
            # REA-010 file is still a build file and is still read back.
            pieces = requirements.get(label, ())
            if not pieces and not declared:
                # Nothing is required on this destination, but its release
                # object is the run's release and therefore carries the clock
                # the pending deadline runs from. Read it (read-only) and keep
                # the destination's historic single ``published`` line.
                if observed_since[0] is None:
                    observed_since[0] = await _destination_clock(
                        adapter, destination, tag_name
                    )
                return False
            try:
                found = await adapter.find_release(destination.repo, tag_name)
                if not isinstance(found, dict) or found.get("id") is None:
                    _fail_verification(
                        report, audit, tag_name, label,
                        [f"release for {tag_name} not found after publication"],
                    )
                    return True
                assets = await adapter.list_release_assets(
                    destination.repo, found["id"]
                )
            except Exception as exc:  # noqa: BLE001 (record — the report, not abort)
                _fail_verification(
                    report, audit, tag_name, label, [f"read-back failed: {exc}"]
                )
                return True
            # The pending deadline runs from the release object's own creation
            # time; a destination that does not report one starts the clock at
            # this run, which can only make a piece pending for longer, never
            # red sooner.
            since = parse_timestamp(found.get("created_at")) or now
            if observed_since[0] is None:
                observed_since[0] = since
            seen.add(label)
            _judge_manifest_pieces(
                report, audit, tag_name, label, manifest, pieces, declared,
                assets, artifacts, since, now,
            )
            return True

        if adapters is not None:
            if len(adapters) != len(destinations):
                raise ValueError(
                    f"publish_release resolved {len(destinations)} destinations but "
                    f"received {len(adapters)} adapters"
                )
            paired: list[tuple[Destination, ForgeAdapter]] = list(
                zip(destinations, adapters)
            )
        elif credentials is not None:
            # P36-002 criterion 3: build per-destination; a missing credential
            # fails only that destination and leaves others running.
            paired = []
            for destination in destinations:
                try:
                    paired.append(
                        (
                            destination,
                            adapters_for(
                                [destination],
                                base_url=base_url,
                                credentials=credentials,
                            )[0],
                        )
                    )
                except MissingCredentialError as exc:
                    label = _destination_label(destination)
                    report.failed.append(label)
                    report.failure_details[label] = str(exc)
                    report.body_states[label] = f"failed: {exc}"
                    logger.error(
                        f"partial publication: {label} "
                        f"failed: {exc}"
                    )
        else:
            paired = list(
                zip(
                    destinations,
                    adapters_for(
                        destinations,
                        token=token,
                        webhook_secret=webhook_secret,
                        base_url=base_url,
                    ),
                )
            )

        # P36-017 criterion 5: RC tag guard. A tag matching vX.Y.Z-rc.N
        # is not created, written or mirrored on any public destination.
        # The destination is skipped with one named log line and the run
        # summary lists the reason.
        _is_rc = _RC_TAG_RE.match(tag_name)

        for destination, adapter in paired:
            label = _destination_label(destination)
            # A release destination without explicit visibility is public
            # (the RC guard must not let an undeclared visibility bypass it).
            _is_public = (
                destination.visibility == "public"
                or (destination.visibility == "" and destination.role == "release")
            )
            if _is_rc and _is_public:
                reason = (
                    f"RC tag {tag_name} skipped on public destination "
                    f"{label} (LVC-305 pending)"
                )
                logger.info(reason)
                report.rc_skipped.append(label)
                report.body_states[label] = "rc_skipped"
                _audit_destination(audit, tag_name, label, "rc-skipped", reason)
                continue
            per_dest_login = _resolve_publisher_login(publisher_login, destination)
            if per_dest_login is None:
                # Per-destination mode and the login is unavailable: fail
                # this destination by name, fail closed (P36-003 design C1).
                logger.error(
                    f"partial publication: {label} failed: no publisher_login "
                    f"for credential_variable {destination.credential_variable!r}"
                )
                report.failed.append(label)
                report.body_states[label] = "failed: publisher_login unavailable"
                report.failure_details[label] = (
                    f"publisher_login unavailable for credential_variable "
                    f"{destination.credential_variable!r}"
                )
                _audit_destination(audit, tag_name, label, "failed",
                                  f"publisher_login unavailable for "
                                  f"credential_variable {destination.credential_variable!r}")
                continue
            # REA-012 (D1): the external-audience gate runs on the FINAL body
            # before any write on this destination, and only this destination
            # is refused when the body names the author's tracker. The gate
            # runs here because the shipped workflow calls publish_release
            # directly and composes the body itself — the gate must be in this
            # path — and per destination because one refused body must not
            # stop another destination whose body is clean.
            if release_body is not None and ticket_prefixes:
                try:
                    check_release_notes_audience(release_body, ticket_prefixes)
                except ReleaseNotesAudienceError as exc:
                    logger.error(f"partial publication: {label} failed: {exc}")
                    report.failed.append(label)
                    report.body_states[label] = f"failed: {exc}"
                    report.failure_details[label] = str(exc)
                    _audit_destination(audit, tag_name, label, "failed", str(exc))
                    continue
            try:
                if skip_existing and await adapter.release_exists(
                    destination.repo, tag_name
                ):
                    await _judge_existing_release(
                        adapter, destination, report, audit, tag_name, label,
                        release_name, release_notes, artifacts, per_dest_login,
                        release_body=release_body,
                    )
                    await _verify(adapter, destination, label)
                    continue
                duplicate_detail = await _publish_to_destination(
                    adapter,
                    destination,
                    tag_name=tag_name,
                    release_name=release_name,
                    release_notes=release_notes,
                    artifacts=artifacts,
                    draft=draft,
                    prerelease=prerelease,
                    body_override=release_body,
                )
                if duplicate_detail is not None:
                    # The create call answered a duplicate: the destination
                    # already holds the release, whether or not the pre-flight
                    # lookup could see it (a draft, or no write access to the
                    # list). Continue with that object, the same judgement as
                    # for a release the lookup showed (REA-011, OE-042a).
                    await _judge_existing_release(
                        adapter, destination, report, audit, tag_name, label,
                        release_name, release_notes, artifacts, per_dest_login,
                        release_body=release_body,
                        duplicate_detail=duplicate_detail,
                    )
                    await _verify(adapter, destination, label)
                    continue
            except Exception as exc:  # noqa: BLE001 (record — the report, not abort)
                logger.error(f"partial publication: {label} failed: {exc}")
                report.failed.append(label)
                report.body_states.setdefault(label, f"failed: {exc}")
                detail = await _conflict_detail(adapter, destination, tag_name, exc)
                if detail:
                    report.failure_details[label] = detail
                _audit_destination(audit, tag_name, label, "failed", detail)
            else:
                report.published.append(label)
                report.body_states[label] = "written"
                if per_dest_login:
                    # The release this call created is the engine's own.
                    report.publishers[label] = "engine"
                # REA-014: check the published set on read-back before the
                # destination counts as published. With nothing declared this
                # skips and the line stays ``result=published``.
                audited = await _verify(adapter, destination, label)
                if not audited:
                    _audit_destination(audit, tag_name, label, "published")

        # REA-016: a manifest destination this run did not visit (D4: the
        # github-built piece on a github destination another workflow owns) is
        # still named. This run holds no evidence about it — it never read
        # that destination — so the piece is pending, never red and never
        # published.
        if manifest is not None and requirements:
            for label, pieces in requirements.items():
                if (
                    label in seen
                    or label in report.failed
                    or label in report.rc_skipped
                ):
                    continue
                # This run read nothing on this destination, so there is no
                # evidence that any piece is absent — only that it is not part
                # of this run. Every piece is pending; an unread destination
                # is never red, and never takes another destination's verdict.
                since = observed_since[0] or now
                pending = [
                    f"{name} producer={producer} since={since.isoformat()}"
                    for name, producer in pieces
                ]
                _record_pending(report, audit, tag_name, label, pending)

        # REA-016: the manifest's channels are judged read-only — a present
        # channel is satisfied, an absent one follows its producer, and an
        # unprobeable one (winget, or any read error) is manual, never a
        # failure. A channel has no release object of its own, so it runs on
        # the run's clock when no release clock was observed.
        if manifest is not None and manifest.channels:
            await _judge_channels(
                report, audit, tag_name, manifest, now, observed_since[0],
                channel_reader,
            )

        if report.partial:
            logger.warning(
                f"release-partial tag={tag_name} repo={repo} "
                f"published={report.published} failed={report.failed}"
            )
        return report


def _audit_destination(
    audit: Optional[list[str]],
    tag_name: str,
    label: str,
    result: str,
    detail: str = "",
) -> None:
    if audit is not None:
        line = f"release-publish tag={tag_name} dest={label} result={result}"
        audit.append(f"{line} {detail}" if detail else line)


def _with_duplicate(duplicate_detail: str, detail: str) -> str:
    """Put the duplicate answer first: ``http=<status>
    existing_release_id=<id>``, then what the existing release holds.

    ``duplicate_detail`` is empty on the pre-flight path, where the line keeps
    exactly the detail it had before (REA-011).
    """
    return f"{duplicate_detail}; {detail}" if duplicate_detail else detail


async def _conflict_detail(
    adapter: ForgeAdapter,
    destination: Destination,
    tag_name: str,
    exc: Exception,
) -> str:
    """Name the HTTP status and the destination's existing release id.

    Only for a destination that answered 409 or 422: ``http=<status>
    existing_release_id=<id>``. The id comes from a lookup that must not hide
    the original failure, so a lookup error or a missing release gives
    ``existing_release_id=unknown``. Any other failure has no detail.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        return ""
    status = exc.response.status_code
    if status not in (409, 422):
        return ""
    release_id: object = "unknown"
    try:
        found = await adapter.find_release(destination.repo, tag_name)
        if isinstance(found, dict) and found.get("id") is not None:
            release_id = found["id"]
    except Exception as lookup_exc:  # noqa: BLE001 (the original error stays the story)
        logger.warning(f"could not look up the existing release of {destination.repo}: {lookup_exc}")
    return f"http={status} existing_release_id={release_id}"


async def _existing_release_problems(
    adapter: ForgeAdapter,
    destination: Destination,
    tag_name: str,
    release_name: str,
    artifacts: list[tuple[str, bytes]],
    release_notes: Optional[str] = None,
) -> list[str]:
    """What differs between the destination's release and this build.

    Compares the title with the gated ``release_name`` and the assets with the
    artifacts of this build: a missing name, and a name attached with another
    digest (or, where the forge exposes no digest, another size). An adapter
    that shows no release details (``find_release`` not a dict) gives no
    problems. Empty list: the existing release is the one this build would
    have made.

    When ``release_notes`` is given, a release whose notes differ (text after
    stripping) is reported as ``notes differ`` so the failure names it.
    """
    found = await adapter.find_release(destination.repo, tag_name)
    if not isinstance(found, dict):
        return []
    problems: list[str] = []
    # Both forges always carry ``body`` (null on GitHub for an empty one); an
    # object without the key is a detail-less stand-in and is not judged.
    if (
        release_notes is not None
        and "body" in found
        and (found["body"] or "").strip() != release_notes.strip()
    ):
        problems.append("notes differ from the notes of this build")
    title = found.get("name")
    if title != release_name:
        problems.append(f"title is {title!r}, expected {release_name!r}")
    missing, conflicts = _plan_attachments(found.get("assets"), artifacts)
    if missing:
        problems.append(f"missing assets: {', '.join(n for n, _ in missing)}")
    problems.extend(conflicts)
    if problems:
        problems.insert(0, f"existing_release_id={found.get('id', 'unknown')}")
    return problems


def _asset_sha256(data: bytes) -> str:
    """The build's own SHA-256, ``sha256:<hex>``, as GitHub reports a digest.

    Free: the bytes are on disk (REL-010), so no asset is ever downloaded to
    compute it.
    """
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _asset_difference(existing: dict, data: bytes) -> Optional[str]:
    """Why an attached asset is not the build's file, or None when it cannot
    be proven different.

    The forge's ``digest`` is compared where it is exposed (GitHub:
    ``sha256:<hex>``); a null or absent digest falls back to ``size``
    (Forgejo reports ``digest: null`` but ``size`` — measured 2026-07-28). An
    asset that carries neither cannot be proven different and stays a no-op;
    nothing is downloaded to try (REL-010).
    """
    digest = existing.get("digest")
    if isinstance(digest, str) and digest:
        expected = _asset_sha256(data)
        if digest.lower() != expected.lower():
            return f"digest is {digest}, expected {expected}"
        return None
    size = existing.get("size")
    if isinstance(size, int) and size != len(data):
        return f"size is {size}, expected {len(data)}"
    return None


def _plan_attachments(
    assets: Iterable[object], artifacts: list[tuple[str, bytes]]
) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Split the build's files into what to upload and what conflicts.

    A name attached with the same digest (or the same size where the forge
    exposes no digest) is a no-op; the same name with another digest is a
    conflict and is never overwritten. Only names absent from the release are
    uploaded. Returns ``(missing, conflicts)``; both in artifact order.
    """
    attached = {
        a.get("name"): a
        for a in (assets or ())
        if isinstance(a, dict) and a.get("name") is not None
    }
    missing: list[tuple[str, bytes]] = []
    conflicts: list[str] = []
    for name, data in artifacts:
        existing = attached.get(name)
        if existing is None:
            missing.append((name, data))
            continue
        difference = _asset_difference(existing, data)
        if difference is not None:
            conflicts.append(f"asset {name} differs ({difference})")
    return missing, conflicts


@dataclass
class _Supplement:
    """What to change on an existing release: its id, the title it carries
    now (None when it already equals the gated title) and the missing files."""

    release_id: object
    old_title: Optional[str]
    missing: list[tuple[str, bytes]]


def _title_key(title: str) -> str:
    """Lower-cased title with every space, hyphen and underscore removed.

    DEVIATION from the criterion wording ("case and whitespace"), by owner
    decision: the real bot title differs from the gated one in a hyphen
    (``ops-engine v3.5.1`` against ``Ops Engine v3.5.1``), so hyphens and
    underscores are ignored as well, and all of them are removed, not
    collapsed. Any other difference keeps the titles apart.
    """
    return re.sub(r"[\s_-]+", "", title).casefold()


def _same_title(a: object, b: str) -> bool:
    """Titles equal ignoring case and whitespace (see :func:`_title_key`)."""
    return isinstance(a, str) and _title_key(a) == _title_key(b)


def _release_author(release: dict) -> str:
    """The login of the account that created ``release``, or ``""``.

    Both forges carry the creating account on the release object itself
    (Forgejo/Gitea ``Release.author``, GitHub ``author``); a release without
    one gives no proof of who made it.
    """
    author = release.get("author")
    if not isinstance(author, dict):
        return ""
    login = author.get("login")
    return login if isinstance(login, str) else ""


def _publisher_of(release: dict, publisher_login: str) -> str:
    """``engine`` only when the release names the caller's account; else ``other``.

    An author that cannot be read never counts as the engine: the engine must
    not touch a release it cannot prove is its own.
    """
    if publisher_login and _release_author(release) == publisher_login:
        return "engine"
    return "other"


def _resolve_publisher_login(
    publisher_login: str | dict[str, str],
    destination: Destination,
) -> str | None:
    """Return the publisher login for *destination*, or ``None`` when the
    caller explicitly signalled per-destination attribution but this
    destination's login is unavailable (fail closed, P36-003 design C1).

    ``publisher_login`` as a plain ``str`` is used for every destination
    (the pre-P36-003 call shape).  As a ``dict`` it maps credential-variable
    names to logins: an empty dict is per-destination mode in which no login
    was derived, and a missing or empty entry for *destination* is a failed
    derivation for that destination.  Both return ``None``, and the caller
    fails that destination by name — fail closed, never a silent fallback.
    """
    if isinstance(publisher_login, dict):
        cred = destination.credential_variable or ""
        return publisher_login.get(cred) or None
    return publisher_login


async def _judge_existing_release(
    adapter: ForgeAdapter,
    destination: Destination,
    report: "PublicationReport",
    audit: Optional[list[str]],
    tag_name: str,
    label: str,
    release_name: str,
    release_notes: str,
    artifacts: list[tuple[str, bytes]],
    publisher_login: str = "",
    release_body: str | None = None,
    duplicate_detail: str = "",
) -> None:
    """Record what ``destination`` already holds for ``tag_name``.

    ``duplicate_detail`` is set when the release was found through a duplicate
    answer to the create call (REA-011): ``http=<status>
    existing_release_id=<id>``. It is carried into the audit line of that
    judgement — skipped, incomplete or supplemented — so the duplicate answer
    is named next to the destination. The pre-flight path passes nothing.

    With ``publisher_login`` given, the release the destination already holds
    is attributed to ``engine`` or ``other`` in ``report.publishers``. An
    author that is another account does NOT exempt the object from judgement:
    decision D1 (which supersedes C1 / P36-003) converges it like any other —
    title, body and missing assets — and never deletes or re-creates it. Only
    a foreign release that already is complete (nothing to write) is recorded
    in ``up_to_date``; the publisher is named per destination either way.

    With ``release_body`` given (P36-017), an existing release whose body is
    empty gets the body supplemented (patched in), and one whose body does not
    contain the tag's CHANGELOG section is left untouched and fails that
    destination with a named error.
    """
    # Attributing the release to a publisher is a report fact, not a write
    # gate: it never exempts the object from convergence (D1 supersedes
    # P36-003). Only a release that NAMES another account is provably
    # someone else's; one without an author is attributed ``other`` but keeps
    # the historical judgement.
    foreign = False
    if publisher_login:
        found = await adapter.find_release(destination.repo, tag_name)
        if isinstance(found, dict) and found:
            publisher = _publisher_of(found, publisher_login)
            report.publishers[label] = publisher
            foreign = publisher == "other" and bool(_release_author(found))
            if foreign:
                logger.info(
                    f"Release {tag_name} on {label} was published by another "
                    "publisher; converging it to the intended state (D1)"
                )
    if release_body is not None:
        if await _try_supplement_body(
            adapter, destination, report, audit, tag_name, label,
            release_name, release_body, release_notes, artifacts,
            duplicate_detail=duplicate_detail,
        ):
            return
        # Body already contains the CHANGELOG section.  Use the existing
        # body as the reference for subsequent notes comparisons so that
        # title and asset corrections still run without a false "notes
        # differ" report (the body is the full composed body, not just
        # the raw release_notes).
        found_body = await adapter.find_release(destination.repo, tag_name)
        if isinstance(found_body, dict) and found_body.get("body"):
            release_notes = found_body["body"]
    if await _try_supplement(
        adapter, destination, report, audit, tag_name, label,
        release_name, release_notes, artifacts, duplicate_detail=duplicate_detail,
    ):
        return
    problems = await _existing_release_problems(
        adapter, destination, tag_name, release_name, artifacts,
        release_notes,
    )
    if problems:
        _record_incomplete(
            report, audit, tag_name, label, problems, duplicate_detail=duplicate_detail
        )
        return
    if foreign:
        # Nothing differs from what this build would write, and the object was
        # authored by another account: recorded up to date without a write.
        # Under D1 that is the only outcome left for a foreign release — an
        # incomplete one is converged above, never left behind.
        logger.info(
            f"Release {tag_name} on {label} already holds the intended state"
        )
        report.up_to_date.append(label)
        report.body_states[label] = "up_to_date"
        _audit_destination(
            audit, tag_name, label, "up-to-date",
            _with_duplicate(duplicate_detail, "publisher=other"),
        )
        return
    logger.info(f"Release {tag_name} already exists on {label}, skipping")
    report.skipped.append(label)
    report.body_states.setdefault(label, "skipped")
    _audit_destination(audit, tag_name, label, "skipped", duplicate_detail)


async def _try_supplement_body(
    adapter: ForgeAdapter,
    destination: Destination,
    report: "PublicationReport",
    audit: Optional[list[str]],
    tag_name: str,
    label: str,
    release_name: str,
    release_body: str,
    release_notes: str,
    artifacts: list[tuple[str, bytes]],
    duplicate_detail: str = "",
) -> bool:
    """Judge an existing release with the body-writer rule (P36-017, REA-012).

    Returns True when the destination was handled (converged, failed, or
    retitled); False means the caller should continue with the existing
    supplement/title/asset logic.

    Rule (REA-012 / D1): an existing release with an empty body is converged —
    the title to ``release_name`` per the template when it differs, the body
    to ``release_body`` (the CHANGELOG section under heading and install
    block), and exactly the files of this build that are not attached. An
    object whose body is already exactly ``release_body`` is retitled when its
    title differs and left alone otherwise. When the body does not contain
    the tag's CHANGELOG section, the destination is failed with a named error
    that names the difference and nothing is written there. Nothing is
    re-created or deleted.
    """
    found = await adapter.find_release(destination.repo, tag_name)
    if not isinstance(found, dict) or found.get("id") is None:
        return False
    existing_body = (found.get("body") or "").strip()

    if not existing_body:
        # REA-012 (D1): converge the object — the title per the template and
        # the body to the CHANGELOG section. The two adapter calls stay
        # single-field writes (the adapter API exposes one field per call),
        # and the title is written first so a failure leaves the body as
        # found. Nothing is deleted or re-created.
        title = found.get("name")
        retitle = not (isinstance(title, str) and title == release_name)
        missing, conflicts = _plan_attachments(found.get("assets"), artifacts)
        if conflicts:
            # A name is attached with another digest: never overwritten, so
            # the destination fails by name and writes nothing at all. The
            # title difference, if any, is named beside it.
            parts = [f"existing_release_id={found['id']}"]
            if retitle:
                parts.append(f"title is {title!r}, expected {release_name!r}")
            detail = "; ".join([*parts, *conflicts])
            logger.error(f"partial publication: {label} failed: {detail}")
            report.failed.append(label)
            report.failure_details[label] = detail
            report.body_states[label] = f"failed: {detail}"
            _audit_destination(
                audit, tag_name, label, "failed",
                _with_duplicate(duplicate_detail, detail),
            )
            return True
        try:
            if retitle:
                await adapter.update_release_title(
                    destination.repo, found["id"], release_name
                )
            await adapter.update_release_body(
                destination.repo, found["id"], release_body
            )
            for name, data in missing:
                await adapter.upload_release_asset(
                    repo_full_name=destination.repo,
                    release_id=found["id"],
                    asset_name=name,
                    data=data,
                )
        except NotImplementedError:
            return False
        parts = [f"existing_release_id={found['id']}"]
        if retitle:
            parts.append(f"title corrected from {title!r} to {release_name!r}")
        parts.append("body supplemented (was empty)")
        if missing:
            parts.append("uploaded: " + ", ".join(n for n, _ in missing))
        detail = "; ".join(parts)
        logger.info(f"Release {tag_name} on {label} converged: {detail}")
        report.supplemented.append(label)
        report.body_states[label] = "supplemented"
        _audit_destination(
            audit, tag_name, label, "supplemented",
            _with_duplicate(duplicate_detail, detail),
        )
        return True

    # P36-017: the tag's CHANGELOG section (the raw notes text) must
    # appear in the existing body. A heading alone that names the tag
    # version is not enough — the body must contain the actual
    # CHANGELOG content.
    if release_notes.strip() not in existing_body:
        detail = (
            f"existing release body does not contain the CHANGELOG section "
            f"for {tag_name}"
        )
        logger.error(f"partial publication: {label} failed: {detail}")
        report.failed.append(label)
        report.failure_details[label] = detail
        report.body_states[label] = f"failed: {detail}"
        _audit_destination(
            audit, tag_name, label, "failed", _with_duplicate(duplicate_detail, detail)
        )
        return True

    # Body already contains the CHANGELOG section: already equal. REA-012
    # (D1): when it is exactly the body this call would write and the title
    # differs, the title is converged to the template and the missing files
    # are attached — the whole object converges, not only an empty one.
    title = found.get("name")
    if existing_body == release_body.strip() and not (
        isinstance(title, str) and title == release_name
    ):
        missing, conflicts = _plan_attachments(found.get("assets"), artifacts)
        if conflicts:
            # Same rule as the empty-body branch: a conflicting name fails the
            # destination by name and nothing is written. The title difference
            # this branch exists for is named beside it.
            detail = "; ".join(
                [
                    f"existing_release_id={found['id']}",
                    f"title is {title!r}, expected {release_name!r}",
                    *conflicts,
                ]
            )
            logger.error(f"partial publication: {label} failed: {detail}")
            report.failed.append(label)
            report.failure_details[label] = detail
            report.body_states[label] = f"failed: {detail}"
            _audit_destination(
                audit, tag_name, label, "failed",
                _with_duplicate(duplicate_detail, detail),
            )
            return True
        try:
            await adapter.update_release_title(
                destination.repo, found["id"], release_name
            )
            for name, data in missing:
                await adapter.upload_release_asset(
                    repo_full_name=destination.repo,
                    release_id=found["id"],
                    asset_name=name,
                    data=data,
                )
        except NotImplementedError:
            return False
        parts = [
            f"existing_release_id={found['id']}",
            f"title corrected from {title!r} to {release_name!r}",
        ]
        if missing:
            parts.append("uploaded: " + ", ".join(n for n, _ in missing))
        detail = "; ".join(parts)
        logger.info(f"Release {tag_name} on {label} converged: {detail}")
        report.supplemented.append(label)
        report.body_states[label] = "supplemented"
        _audit_destination(
            audit, tag_name, label, "supplemented",
            _with_duplicate(duplicate_detail, detail),
        )
        return True

    # The body_states entry is recorded here, but we return False so the
    # caller continues with the existing supplement/title/asset logic
    # (P36-017/P36-003). The body_states entry is kept even if the
    # subsequent supplement path overwrites it.
    report.body_states[label] = "already_equal"
    return False


async def _try_supplement(
    adapter: ForgeAdapter,
    destination: Destination,
    report: "PublicationReport",
    audit: Optional[list[str]],
    tag_name: str,
    label: str,
    release_name: str,
    release_notes: str,
    artifacts: list[tuple[str, bytes]],
    duplicate_detail: str = "",
) -> bool:
    """Supplement the existing release of ``destination`` when the rule applies.

    Shared by the existing-release path and the duplicate-on-create path, so
    both run the same logic. Returns True when the destination was
    supplemented and recorded; False leaves the judgement to the caller.
    """
    supplement = await _plan_supplement(
        adapter, destination, tag_name, release_name, release_notes, artifacts
    )
    if supplement is None:
        return False
    detail = await _apply_supplement(
        adapter, destination, release_name, artifacts, supplement
    )
    logger.info(f"Release {tag_name} on {label} supplemented: {detail}")
    report.supplemented.append(label)
    report.body_states.setdefault(label, "supplemented")
    _audit_destination(
        audit, tag_name, label, "supplemented", _with_duplicate(duplicate_detail, detail)
    )
    return True


async def _plan_supplement(
    adapter: ForgeAdapter,
    destination: Destination,
    tag_name: str,
    release_name: str,
    release_notes: str,
    artifacts: list[tuple[str, bytes]],
) -> Optional[_Supplement]:
    """Decide whether an existing release is to be supplemented (OE-042a).

    The rule: the release holds exactly the notes of this build (text equal
    after stripping) and a title equal to the gated ``release_name`` ignoring
    case and whitespace (hyphens and underscores are ignored too).
    Then the title is to be corrected and the files of the build that are not
    attached uploaded. Returns None when the rule does not
    apply (a different note or title, no release details, or nothing to
    change), so the caller judges the release as before: a complete one is
    skipped, any other difference fails the destination with the difference
    named. Attached files are never part of the plan.
    """
    found = await adapter.find_release(destination.repo, tag_name)
    if not isinstance(found, dict) or found.get("id") is None:
        return None
    if (found.get("body") or "").strip() != release_notes.strip():
        return None
    title = found.get("name")
    if not _same_title(title, release_name):
        return None
    missing, conflicts = _plan_attachments(found.get("assets"), artifacts)
    if conflicts:
        # A name attached with another digest is never overwritten: leave the
        # judgement to the caller, which fails the destination by name.
        return None
    retitle = title != release_name
    if not retitle and not missing:
        return None
    return _Supplement(found["id"], title if retitle else None, missing)


async def _apply_supplement(
    adapter: ForgeAdapter,
    destination: Destination,
    release_name: str,
    artifacts: list[tuple[str, bytes]],
    plan: _Supplement,
) -> str:
    """Carry out a plan: correct the title, then upload the missing files.

    Nothing is created or deleted. Any error propagates, so the caller records
    the destination as failed; a rerun plans only what is still missing.
    Returns the audit detail naming the title correction and the files.
    """
    parts = [f"existing_release_id={plan.release_id}"]
    if plan.old_title is not None:
        await adapter.update_release_title(
            destination.repo, plan.release_id, release_name
        )
        parts.append(f"title corrected from {plan.old_title!r} to {release_name!r}")
    for name, data in plan.missing:
        await adapter.upload_release_asset(
            repo_full_name=destination.repo,
            release_id=plan.release_id,
            asset_name=name,
            data=data,
        )
    if plan.missing:
        parts.append("uploaded: " + ", ".join(n for n, _ in plan.missing))
    return "; ".join(parts)


def _record_incomplete(
    report: "PublicationReport",
    audit: Optional[list[str]],
    tag_name: str,
    label: str,
    problems: list[str],
    duplicate_detail: str = "",
) -> None:
    detail = "; ".join(problems)
    logger.error(f"partial publication: {label} holds an incomplete release: {detail}")
    report.failed.append(label)
    report.failure_details[label] = f"incomplete existing release ({detail})"
    report.body_states[label] = f"incomplete: {detail}"
    _audit_destination(
        audit, tag_name, label, "incomplete", _with_duplicate(duplicate_detail, detail)
    )


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
                + "".join(
                    f"\n{label}: {detail}"
                    for label, detail in report.failure_details.items()
                )
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
    body_override: str | None = None,
) -> Optional[str]:
    """Create the release on one destination and attach every artifact to it.

    Returns None when the release was created and its artifacts attached. A
    create call the forge answers as a duplicate (``adapter.release_conflict``:
    409 on Forgejo, 422 on GitHub) means the destination already holds the
    release in some state the by-tag lookup did not show: nothing is attached
    and the duplicate detail (``http=<status> existing_release_id=<id>``) is
    returned, so the caller continues with the existing object. Only the create
    call is read this way; an upload error stays a failure.

    ``body_override``, when given, is used as the release body instead of
    ``release_notes``, so a release created here carries the full body with
    install block and assets line (P36-017).

    The release id comes from ``create_release``'s return value, so the uploads
    target the release object just created — no second lookup and no
    re-download. An exception anywhere in the create-or-attach sequence fails
    the whole destination, which the caller records as a partial publication.
    """
    body = body_override if body_override is not None else release_notes
    try:
        created = await adapter.create_release(
            repo_full_name=destination.repo,
            tag_name=tag_name,
            name=release_name,
            body=body,
            draft=draft,
            prerelease=prerelease,
        )
    except Exception as exc:  # noqa: BLE001 (only a duplicate answer is absorbed)
        # ``is True``: only an adapter's explicit yes absorbs the error.
        if await adapter.release_conflict(exc, destination.repo, tag_name) is True:
            # The duplicate answer names its status; the release id comes from
            # the same lookup ``_conflict_detail`` runs for any 409/422.
            return await _conflict_detail(adapter, destination, tag_name, exc)
        raise
    release_id = created["id"]
    for asset_name, data in artifacts:
        await adapter.upload_release_asset(
            repo_full_name=destination.repo,
            release_id=release_id,
            asset_name=asset_name,
            data=data,
        )
    return None


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


# REA-014: declaration types whose file is an asset of the release object.
# ``ghcr_image`` and ``capacium_exchange`` are published elsewhere (REA-016
# owns their fan-out), so there is nothing on the release to read back.
_RELEASE_ASSET_TYPES = ("github_release_tarball", "github_release_zip")


def _render_asset_name(name: str, tag_name: str) -> str:
    """Render the ``<version>``/``<tag>`` placeholders of a declaration name.

    ``<version>`` is the tag without its leading ``v`` and ``<tag>`` the tag
    itself, the two forms the fleet writes in ``.ops.yaml``.
    """
    version = tag_name[1:] if tag_name.startswith("v") else tag_name
    return name.replace("<version>", version).replace("<tag>", tag_name)


def _declared_release_asset_names(
    config: OpsEngineConfig,
    repo: str,
    repo_dir: str | Path | None,
    tag_name: str,
) -> list[str]:
    """The release-object files the repo declares, names rendered from the tag.

    Read through the same path the destinations come from: Layer 2 merged with
    the Layer-3 ``.ops.yaml`` in ``repo_dir``. An absent declaration and
    ``assets: none`` both mean "no requirement" and give an empty list, so
    nothing is verified and the run behaves as before. Declarations that are
    not release-object assets are dropped here.
    """
    org, _, name = repo.partition("/")
    layer2 = config.get_repo_config(org, name)
    if repo_dir is not None:
        layer3 = load_ops_yaml(repo_dir)
        if layer3 is not None:
            layer2 = layer2.merge_layer3(layer3)
    release_config = layer2.release
    if release_config is None:
        return []
    declarations = release_config.assets
    if not declarations:
        return []
    names: list[str] = []
    for declaration in declarations:
        if declaration.type not in _RELEASE_ASSET_TYPES or not declaration.name:
            continue
        names.append(_render_asset_name(declaration.name, tag_name))
    return names


def _resolved_manifest(
    config: OpsEngineConfig,
    repo: str,
    repo_dir: str | Path | None,
) -> Optional[AssetManifest]:
    """The repo's ``release.manifest``, Layer 2 merged with its ``.ops.yaml``.

    The same path the destinations and the REA-014 declaration come from, so
    a manifest declared in the repository at the tag is the one judged. None
    means the repo declares no manifest: every asset is forgejo-produced and
    required at once, exactly the REA-014 behaviour (backward compatible).
    """
    org, _, name = repo.partition("/")
    layer2 = config.get_repo_config(org, name)
    if repo_dir is not None:
        layer3 = load_ops_yaml(repo_dir)
        if layer3 is not None:
            layer2 = layer2.merge_layer3(layer3)
    release_config = layer2.release
    if release_config is None:
        return None
    return release_config.manifest


async def _destination_clock(
    adapter: ForgeAdapter,
    destination: Destination,
    tag_name: str,
) -> Optional[datetime]:
    """Read ONE destination's own release clock, or None when it cannot be read.

    The pending deadline runs from the release object's own ``created_at``.
    An unread clock is None — the caller must leave the destination pending,
    never substitute another destination's clock and never assume red. Any
    read error answers None: the clock is evidence, not a gate.
    """
    try:
        found = await adapter.find_release(destination.repo, tag_name)
    except Exception:  # noqa: BLE001 (the clock is a courtesy, never a gate)
        return None
    if not isinstance(found, dict):
        return None
    return parse_timestamp(found.get("created_at"))


def _producer_destination(producer: str, repo: str) -> str:
    """Where a piece with no ``destinations`` is expected: its own forge."""
    return f"{producer}:{repo}"


def _manifest_requirements(
    manifest: AssetManifest,
    repo: str,
    tag_name: str,
) -> dict[str, tuple[tuple[str, str], ...]]:
    """Group the manifest's release-object assets by destination label.

    Each entry is ``(rendered name, producer)``. An asset with no explicit
    ``destinations`` is expected on its producer's own forge (D4), so a
    github-produced binary does not burden the forgejo destination. Channel
    pieces are judged read-only by :mod:`ops_engine.modules.asset_manifest`,
    never here: they are not on a release object.
    """
    requirements: dict[str, list[tuple[str, str]]] = {}
    for entry in manifest.assets:
        name = _render_asset_name(entry.name, tag_name)
        targets = entry.destinations or [_producer_destination(entry.producer, repo)]
        for label in targets:
            requirements.setdefault(label, []).append((name, entry.producer))
    return {label: tuple(pieces) for label, pieces in requirements.items()}


def _judge_manifest_pieces(
    report: "PublicationReport",
    audit: Optional[list[str]],
    tag_name: str,
    label: str,
    manifest: AssetManifest,
    pieces: tuple[tuple[str, str], ...],
    declared: list[str],
    assets: Iterable[object],
    artifacts: list[tuple[str, bytes]],
    since: datetime,
    now: datetime,
) -> None:
    """Judge one destination's manifest pieces against the deadline.

    A piece the destination holds is verified like any declared file; a
    forgejo-produced piece is required at once, so a missing one fails the
    destination by name. A github-produced piece missing inside the deadline
    leaves the destination pending (D5), red only once the deadline passed.
    """
    attached = {
        asset.get("name"): asset
        for asset in assets or ()
        if isinstance(asset, dict) and asset.get("name") is not None
    }
    requirements = {name: producer for name, producer in pieces}
    problems: list[str] = []
    pending: list[str] = []
    entries: list[str] = []
    # The manifest may extend the REA-014 declaration; a declared file that is
    # not a manifest piece is still read back as before.
    for name in declared:
        if name in requirements:
            continue
        existing = attached.get(name)
        if existing is None:
            problems.append(f"{name} missing")
            continue
        data = dict(artifacts).get(name)
        if data is None:
            entries.append(name)
            continue
        difference = _asset_difference(existing, data)
        if difference is not None:
            problems.append(f"{name} differs ({difference})")
            continue
        entries.append(f"{name} {_asset_sha256(data)}")
    for name, producer in requirements.items():
        existing = attached.get(name)
        if existing is None:
            if producer == "github" and not deadline_passed(
                since, manifest.deadline_minutes, now
            ):
                pending.append(
                    f"{name} producer={producer} since={since.isoformat()}"
                )
                continue
            problems.append(f"{name} missing producer={producer}")
            continue
        data = dict(artifacts).get(name)
        if data is None:
            entries.append(name)
            continue
        difference = _asset_difference(existing, data)
        if difference is not None:
            problems.append(f"{name} differs ({difference})")
            continue
        entries.append(f"{name} {_asset_sha256(data)}")
    if problems:
        _fail_manifest(report, audit, tag_name, label, problems)
        return
    if entries:
        report.verified[label] = entries
    if pending:
        _record_pending(report, audit, tag_name, label, pending)
        return
    if entries:
        _audit_destination(audit, tag_name, label, "verified", ", ".join(entries))


def _fail_manifest(
    report: "PublicationReport",
    audit: Optional[list[str]],
    tag_name: str,
    label: str,
    problems: list[str],
) -> None:
    """Fail a destination whose manifest piece is absent or wrong.

    The detail names the piece, its producer and the destination, so an
    operator reading the report alone knows all three.
    """
    detail = (
        f"manifest not satisfied on {label}: " + "; ".join(problems)
    )
    logger.error(f"partial publication: {label} failed: {detail}")
    report.failed.append(label)
    report.failure_details[label] = detail
    report.body_states.setdefault(label, f"failed: {detail}")
    for bucket in (
        report.published,
        report.skipped,
        report.supplemented,
        report.up_to_date,
    ):
        if label in bucket:
            bucket.remove(label)
    report.verified.pop(label, None)
    report.pending.pop(label, None)
    _audit_destination(audit, tag_name, label, "manifest-failed", detail)


def _record_pending(
    report: "PublicationReport",
    audit: Optional[list[str]],
    tag_name: str,
    label: str,
    pending: list[str],
) -> None:
    """Record a destination whose pieces are still on their way: not a
    failure, but a state an operator can read."""
    report.pending[label] = pending
    _audit_destination(audit, tag_name, label, "pending", "; ".join(pending))


async def _judge_channels(
    report: "PublicationReport",
    audit: Optional[list[str]],
    tag_name: str,
    manifest: AssetManifest,
    now: datetime,
    since: Optional[datetime],
    channel_reader: object,
) -> None:
    """Judge the manifest's channels read-only and fold them into the report.

    A satisfied channel gets its own audit line. A pending or failed channel
    is recorded under its ``<channel>:<name>`` label, so an operator sees the
    channel as the subject and, for a failure, the producer and the channel
    form in the same detail. Manual (unprobeable) channels are audited as
    such and are never a failure — unknown is not a verdict.
    """
    unsatisfied: list[str] = []
    verdict = await evaluate_channels(
        manifest,
        tag_name,
        reader=channel_reader if channel_reader is not None else HttpxChannelReader(),
        since=since,
        now=now,
        unsatisfied=unsatisfied,
    )
    for label in verdict.satisfied:
        _audit_destination(audit, tag_name, label, "channel-satisfied")
    for label in verdict.manual:
        _audit_destination(audit, tag_name, label, "channel-manual")
    for piece in verdict.pending:
        label = f"{piece.channel}:{piece.name}"
        _record_pending(report, audit, tag_name, label, [piece.as_text()])
    for piece in verdict.failed:
        label = f"{piece.channel}:{piece.name}"
        _fail_manifest(report, audit, tag_name, label, [piece.as_text()])
    if unsatisfied:
        # A forgejo-produced channel is red on absence at once.
        for entry in unsatisfied:
            label = entry.split(" ", 1)[0]
            _fail_manifest(report, audit, tag_name, label, [entry])


async def _verify_destination_assets(
    adapter: ForgeAdapter,
    destination: Destination,
    report: "PublicationReport",
    audit: Optional[list[str]],
    tag_name: str,
    label: str,
    declared: list[str],
    artifacts: list[tuple[str, bytes]],
) -> bool:
    """Read the destination's release back and check every declared file.

    The asset set comes from ``list_release_assets`` on the release found by
    tag (REA-013), never from a download (REL-010): the build's bytes are on
    disk, so a missing name or another digest/size is proven locally. On
    success the destination is recorded in ``report.verified`` as
    ``"<name> sha256:<hex>"`` per file, and the audit line reads
    ``result=verified``. On failure the destination leaves ``published`` /
    ``skipped`` / ``supplemented`` / ``up_to_date`` and is recorded in
    ``failed`` with a detail naming each failing file (and both digests where
    the forge exposes one); the audit line reads ``result=verify-failed``.
    """
    try:
        found = await adapter.find_release(destination.repo, tag_name)
        if not isinstance(found, dict) or found.get("id") is None:
            return _fail_verification(
                report, audit, tag_name, label,
                [f"release for {tag_name} not found after publication"],
            )
        assets = await adapter.list_release_assets(destination.repo, found["id"])
    except Exception as exc:  # noqa: BLE001 (record — the report, not abort)
        return _fail_verification(
            report, audit, tag_name, label, [f"read-back failed: {exc}"]
        )
    attached = {
        asset.get("name"): asset
        for asset in assets or ()
        if isinstance(asset, dict) and asset.get("name") is not None
    }
    build = dict(artifacts)
    problems: list[str] = []
    entries: list[str] = []
    for name in declared:
        existing = attached.get(name)
        if existing is None:
            problems.append(f"{name} missing")
            continue
        data = build.get(name)
        if data is None:
            # Attached, but this run's build does not carry it: there is no
            # local truth to compare against, so it cannot be proven bad.
            entries.append(name)
            continue
        difference = _asset_difference(existing, data)
        if difference is not None:
            problems.append(f"{name} differs ({difference})")
            continue
        entries.append(f"{name} {_asset_sha256(data)}")
    if problems:
        return _fail_verification(report, audit, tag_name, label, problems)
    report.verified[label] = entries
    _audit_destination(audit, tag_name, label, "verified", ", ".join(entries))
    return True


def _fail_verification(
    report: "PublicationReport",
    audit: Optional[list[str]],
    tag_name: str,
    label: str,
    problems: list[str],
) -> bool:
    """Record a destination whose asset set failed the read-back."""
    detail = "verification failed: " + "; ".join(problems)
    logger.error(f"partial publication: {label} failed: {detail}")
    report.failed.append(label)
    report.failure_details[label] = detail
    report.body_states.setdefault(label, f"failed: {detail}")
    for bucket in (
        report.published,
        report.skipped,
        report.supplemented,
        report.up_to_date,
    ):
        if label in bucket:
            bucket.remove(label)
    report.verified.pop(label, None)
    _audit_destination(audit, tag_name, label, "verify-failed", detail)
    return False


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

    try:
        changelog = await adapter.get_file_content(
            repo, config.changelog_path, ref=tag_name
        )
    except Exception as exc:  # noqa: BLE001 (a named refusal, not a fallback)
        raise MissingChangelogEntryError(
            f"MissingChangelogEntryError: could not read {config.changelog_path} "
            f"of {repo} at {tag_name}: {exc}. No notes, so no release."
        ) from exc

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

        # P36-017: read the per-repository declaration for the body parts
        # (install block and assets line) from the .ops.yaml at the tag.
        # These are business data, never engine defaults.
        layer3_config = load_ops_yaml(layer3_dir)
        install_block = (
            layer3_config.release_body_install_block
            if layer3_config is not None
            else ""
        )
        assets_line = (
            layer3_config.release_body_assets_line
            if layer3_config is not None
            else ""
        )

        # P36-021: the title form. The repository's own release block (Layer 3)
        # wins, then the layover's release block (Layer 2), then the derived
        # default. The template is resolved AFTER the .ops.yaml read so the
        # repository declaration is what the gate sees, and the gate still runs
        # before the CHANGELOG section is extracted (OE-007).
        layer3_release = layer3_config.release if layer3_config is not None else None
        name_template = (
            context.name_template
            or (layer3_release.name_template if layer3_release is not None else None)
            or getattr(config, "name_template", None)
            or DEFAULT_NAME_TEMPLATE
        )
        title_form = render_release_name(name_template, repo, "{tag_name}")
        try:
            check_release_title(tag_name, name_template=title_form)
        except ReleaseTitleError as exc:
            # Criterion 2: the refusal names the repository whose release block
            # declared the template. Only this repository fails; the others of
            # the same run keep their own loaded block and keep running.
            raise ReleaseTitleError(f"{repo}: {exc}") from exc

        notes = extract_changelog_section(changelog, tag_name)

        identity = context.identity

        # Compose the full release body (P36-017).  The install block and
        # assets line come from the per-repository declaration at the tag,
        # never from engine defaults.
        canonical_url = identity.canonical_url.removesuffix(".git")
        changelog_url = (
            f"{canonical_url}/src/tag/{tag_name}/{config.changelog_path}"
        )
        body = f"## {render_release_name(name_template, repo, tag_name)}\n\n{notes}\n"
        if install_block:
            body += f"### Install\n{install_block}\n\n"
        if assets_line:
            body += f"{assets_line}\n\n"
        body += (
            f"See [{config.changelog_path}]({changelog_url})"
            f" for all changes.\n"
        )

        # The external-audience gate runs on the FINAL body before any
        # destination write (P36-017 criterion 3). A tracker reference
        # introduced only by the install block is refused here.
        check_release_notes_audience(body, context.ticket_prefixes)

        # Identity (OE-008), after every gate.
        await push_canonical_tag(
            identity.repo_dir,
            tag_name,
            identity.canonical_url,
            identity.canonical_token,
            identity.mirror_remote,
            identity.runner,
            mirror_token=identity.mirror_token,
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
            credentials=context.credentials,
            repo_dir=layer3_dir,
            roles=("release",),
            skip_existing=True,
            audit=context.audit,
            publisher_login=context.publisher_login,
            release_body=body,
            channel_reader=context.channel_reader,
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
