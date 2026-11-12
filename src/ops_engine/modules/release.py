"""CORE-002: ReleaseHandler — Create releases from tag pushes or labeled merges."""

import fnmatch
import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx

from ops_engine.adapters.base import ForgeAdapter
from ops_engine.adapters.factory import Credential, MissingCredentialError, adapters_for
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
    # Destinations whose existing release (made by someone else, with the notes
    # of this build) got its title corrected and its missing files uploaded.
    # Not failures: they are not in ``failed``.
    supplemented: list[str] = field(default_factory=list)
    # label -> what an operator needs to read: the HTTP status and the
    # destination's existing release id (409/422), or what differs on an
    # existing release (incomplete). Both labels are also in ``failed``.
    failure_details: dict[str, str] = field(default_factory=dict)
    # Destinations whose release for the tag already exists from ANOTHER
    # publisher: recorded as up to date, never edited and never duplicated.
    # Not failures: they are not in ``failed``.
    up_to_date: list[str] = field(default_factory=list)
    # destination label -> who made the release found for the tag there:
    # ``engine`` (the account named by ``publisher_login``) or ``other``.
    publishers: dict[str, str] = field(default_factory=dict)

    @property
    def publisher(self) -> str:
        """Who made the release found for this tag, when that is one answer.

        A tag-level report names ``engine`` or ``other`` only when every
        attributed destination agrees; no attribution (or a disagreement
        between destinations) is the empty string, never a guess.
        """
        values = set(self.publishers.values())
        return values.pop() if len(values) == 1 else ""

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
        adapter (each destination answers for itself), and a create call the
        forge answers as a duplicate (409 Forgejo, 422 GitHub) is recorded the
        same way, so a release the by-tag lookup cannot see (a draft) is not a
        failure. It is opt-in so callers
        that publish to a known-empty destination keep their exact call
        sequence.

        ``audit`` follows the ``MirrorHandler.push_event`` convention: when a
        list is given, one line per destination is appended to it
        (``release-publish tag=... dest=... result=published|failed|skipped|incomplete``).
        A destination skipped because its release already exists is not a
        failure, unless that release is incomplete (its title is not the gated
        title, or it lacks assets of this build): it is then recorded in
        ``failed`` and ``incomplete`` names what differs. A failure the
        destination answered with 409 or 422 carries ``http=<status>
        existing_release_id=<id>``, in the audit line and in the notification.

        An existing release with the notes of this build (equal after
        stripping) and a title equal to the gated title ignoring case and
        whitespace is supplemented, not failed: the title is corrected and
        exactly the missing files are uploaded; it is listed in
        ``supplemented`` and the audit line reads ``result=supplemented``.
        Files already attached are untouched; nothing is re-created or deleted. When any destination failed, one warning line is logged for
        the whole call (the engine has no event log, D20).

        ``publisher_login`` is the login the engine publishes as (P36-003). A
        repository can have two publishers — the engine and a repository-local
        workflow — so a release that already exists on a destination is only
        the engine's to touch when the release object's own ``author.login``
        names that account. When it names another account the release is
        recorded in ``up_to_date`` and neither edited nor duplicated; the
        report's ``publisher`` says ``engine`` or ``other``. Without the
        parameter the engine cannot attribute a release and every release found
        is judged as before. An undeterminable author degrades to ``other``,
        never to a false ``engine``.

        A ``dict[str, str]`` maps credential-variable names to logins (one
        login per destination, P36-003 design C1). An empty dict means no
        per-destination mode. A non-empty dict where a destination's
        credential_variable is absent or maps to an empty string means the
        derivation failed for that destination: it is failed by name and
        nothing is supplemented, edited or duplicated (fail closed).

        ``credentials`` is a per-destination credential mapping (ADP-011):
        each destination's forge value maps to its own :class:`Credential`,
        so a Forgejo destination never receives the GitHub token and vice
        versa (P36-002). Omitted or ``None`` falls back to the legacy shared
        ``token``/``webhook_secret`` pair.

        Raises:
            ValueError: a supplied ``adapters`` list does not match the resolved
                destination count.
        """
        destinations = _resolve_publish_destinations(config, repo, repo_dir, roles)

        release_name = render_release_name(
            name_template or DEFAULT_NAME_TEMPLATE, repo, tag_name
        )
        artifacts = _collect_artifacts(artifact_dir)

        report = PublicationReport(repo=repo, tag_name=tag_name)

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

        for destination, adapter in paired:
            label = _destination_label(destination)
            per_dest_login = _resolve_publisher_login(publisher_login, destination)
            if per_dest_login is None:
                # Per-destination mode and the login is unavailable: fail
                # this destination by name, fail closed (P36-003 design C1).
                logger.error(
                    f"partial publication: {label} failed: no publisher_login "
                    f"for credential_variable {destination.credential_variable!r}"
                )
                report.failed.append(label)
                report.failure_details[label] = (
                    f"publisher_login unavailable for credential_variable "
                    f"{destination.credential_variable!r}"
                )
                _audit_destination(audit, tag_name, label, "failed",
                                  f"publisher_login unavailable for "
                                  f"credential_variable {destination.credential_variable!r}")
                continue
            try:
                if skip_existing and await adapter.release_exists(
                    destination.repo, tag_name
                ):
                    await _judge_existing_release(
                        adapter, destination, report, audit, tag_name, label,
                        release_name, release_notes, artifacts, per_dest_login,
                    )
                    continue
                created = await _publish_to_destination(
                    adapter,
                    destination,
                    tag_name=tag_name,
                    release_name=release_name,
                    release_notes=release_notes,
                    artifacts=artifacts,
                    draft=draft,
                    prerelease=prerelease,
                    tolerate_existing=skip_existing,
                )
                if not created:
                    # The create call answered a duplicate: the release was
                    # created between the lookup and the create. Same rule as
                    # for a release the lookup showed (OE-042a).
                    await _judge_existing_release(
                        adapter, destination, report, audit, tag_name, label,
                        release_name, release_notes, artifacts, per_dest_login,
                    )
                    continue
            except Exception as exc:  # noqa: BLE001 (record — the report, not abort)
                logger.error(f"partial publication: {label} failed: {exc}")
                report.failed.append(label)
                detail = await _conflict_detail(adapter, destination, tag_name, exc)
                if detail:
                    report.failure_details[label] = detail
                _audit_destination(audit, tag_name, label, "failed", detail)
            else:
                report.published.append(label)
                if per_dest_login:
                    # The release this call created is the engine's own.
                    report.publishers[label] = "engine"
                _audit_destination(audit, tag_name, label, "published")

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

    Compares the title with the gated ``release_name`` and the attached asset
    names with the artifacts of this build. An adapter that shows no release
    details (``find_release`` not a dict) gives no problems. Empty list: the
    existing release is the one this build would have made.

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
    attached = {
        a.get("name") for a in (found.get("assets") or []) if isinstance(a, dict)
    }
    missing = [name for name, _ in artifacts if name not in attached]
    if missing:
        problems.append(f"missing assets: {', '.join(missing)}")
    if problems:
        problems.insert(0, f"existing_release_id={found.get('id', 'unknown')}")
    return problems


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
    names to logins; an empty dict means no per-destination mode (backward
    compat), a non-empty dict with a missing or empty entry for
    *destination* means the derivation failed for that destination.
    """
    if isinstance(publisher_login, dict):
        if not publisher_login:
            return ""
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
) -> None:
    """Record what ``destination`` already holds for ``tag_name``.

    With ``publisher_login`` given, a release the destination already holds and
    that names ANOTHER account as its author is one publisher's release per
    repository (P36-003): recorded in ``up_to_date``, neither edited nor
    duplicated, and the publisher is named per destination. Any other release
    is judged as before — supplemented, incomplete, or skipped.
    """
    if publisher_login:
        found = await adapter.find_release(destination.repo, tag_name)
        if isinstance(found, dict) and found:
            publisher = _publisher_of(found, publisher_login)
            report.publishers[label] = publisher
            # Only a release that NAMES another account is someone else's; one
            # without an author proves nothing and is judged as before.
            if publisher == "other" and _release_author(found):
                logger.info(
                    f"Release {tag_name} on {label} was published by another "
                    "publisher, leaving it untouched"
                )
                report.up_to_date.append(label)
                _audit_destination(
                    audit, tag_name, label, "up-to-date", f"publisher={publisher}"
                )
                return
    if await _try_supplement(
        adapter, destination, report, audit, tag_name, label,
        release_name, release_notes, artifacts,
    ):
        return
    problems = await _existing_release_problems(
        adapter, destination, tag_name, release_name, artifacts,
        release_notes,
    )
    if problems:
        _record_incomplete(report, audit, tag_name, label, problems)
        return
    logger.info(f"Release {tag_name} already exists on {label}, skipping")
    report.skipped.append(label)
    _audit_destination(audit, tag_name, label, "skipped")


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
    _audit_destination(audit, tag_name, label, "supplemented", detail)
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
    attached = {
        a.get("name") for a in (found.get("assets") or []) if isinstance(a, dict)
    }
    missing = [(n, d) for n, d in artifacts if n not in attached]
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
) -> None:
    detail = "; ".join(problems)
    logger.error(f"partial publication: {label} holds an incomplete release: {detail}")
    report.failed.append(label)
    report.failure_details[label] = f"incomplete existing release ({detail})"
    _audit_destination(audit, tag_name, label, "incomplete", detail)


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
    tolerate_existing: bool = False,
) -> bool:
    """Create the release on one destination and attach every artifact to it.

    Returns True when the release was created and its artifacts attached. With
    ``tolerate_existing``, a create call the forge answers as a duplicate
    (``adapter.release_conflict``: 409 on Forgejo, 422 on GitHub) means the
    destination already holds the release in some state the by-tag lookup did
    not show: nothing is attached and False is returned. Only the create call
    is read this way; an upload error stays a failure.

    The release id comes from ``create_release``'s return value, so the uploads
    target the release object just created — no second lookup and no
    re-download. An exception anywhere in the create-or-attach sequence fails
    the whole destination, which the caller records as a partial publication.
    """
    try:
        created = await adapter.create_release(
            repo_full_name=destination.repo,
            tag_name=tag_name,
            name=release_name,
            body=release_notes,
            draft=draft,
            prerelease=prerelease,
        )
    except Exception as exc:  # noqa: BLE001 (only a duplicate answer is absorbed)
        # ``is True``: only an adapter's explicit yes absorbs the error.
        if (
            tolerate_existing
            and await adapter.release_conflict(exc, destination.repo, tag_name) is True
        ):
            return False
        raise
    release_id = created["id"]
    for asset_name, data in artifacts:
        await adapter.upload_release_asset(
            repo_full_name=destination.repo,
            release_id=release_id,
            asset_name=asset_name,
            data=data,
        )
    return True


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
