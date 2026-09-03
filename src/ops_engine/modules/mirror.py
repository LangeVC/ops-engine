"""CORE-004: MirrorHandler — Verify mirror sync after push to primary forge.

OME-002 resolution contract (public surface, additive on ``MirrorHandler``):

The mirror destination is resolved by a strict precedence, never by a blind
inline fallback. A silently-wrong destination surfaces as a git exit 128 deep
inside a push; the contract instead fails at a named preflight, before any
push, naming the variable to set and the value it expected.

Resolution order:
  1. an explicit per-repository override wins  (the exception mechanism);
  2. otherwise the organisation declaration composes the destination
     (``org.github.login`` + ``/`` + for the forge repo name);
  3. otherwise the run FAILS — names the variable and the value it expected.

A fallback is only ever a GATED fallback: it is computed as a candidate, then
proven with two independent proofs before it may be used. The two proofs
answer different questions:

  EXISTS   ``git ls-remote`` proves the repository is there and readable. It
           is the operator's premise — the mirror must FIND a destination,
           never bring one into being. Nothing in this contract creates a
           repository.

  IS OURS  reachability is not ownership: a public repo owned by someone else
           whose name happens to match reads fine, so EXISTS vouches for it,
           and the push then dies for want of write permission. This proof
           establishes the resolved destination is one we MAY WRITE TO and
           fails at the same named preflight when it is not.

Neither proof creates a repository under any outcome. Both use real git, not
heuristics in this repository.

OME-009 default-branch blocker (additive on ``MirrorHandler``):

A refusal of the DEFAULT branch is a release blocker, and the gate now says so
instead of reporting a dropped ref that only surfaces two steps later as a
different forge's hard-gate failure. A refused default branch is not the gate
working — it strands every release that follows. ``MirrorHandler.gate_ref``
distinguishes the two cases without deciding what happens next: it is pure
classification plus a named ``DefaultBranchBlockedError`` on the default-branch
case. Nothing in this contract retries, and nothing overrides a refusal — the
break-glass remains a deliberate operator action, never an automatic push of a
ref the gate refused.
"""

import asyncio
import fnmatch
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from ops_engine.adapters.base import ForgeAdapter
from ops_engine.config_loader import MirrorConfig, VisibilityConfig

logger = logging.getLogger(__name__)

# The repository variable (repo or org scope) whose value, when set, is the
# per-repository override. Repo scope wins over org scope (measured: run 38407 /
# job 68398, a PLATFORM rule on this Forgejo instance, not a repo property).
MIRROR_VARIABLE = "GH_REPOSITORY"

# The path classes that may carry planning substrate. These are a CANDIDATE
# filter, not the decision: a file is only inspected for substrate content when
# its path falls into one of these classes. The decision itself is content-based
# (see :meth:`MirrorHandler.classify_visibility`). The arms mirror the deployed
# gate's BLOCKED_PATHS so a layover can feed the same candidate set, but here
# they only narrow *where* content is measured — never the verdict.
_VISIBILITY_CANDIDATE_GLOBS: tuple[str, ...] = (
    ".skillweave/**",
    "strategy.md",
    "**/strategy.md",
    "prd*.md",
    "**/prd*.md",
    "prd*.json",
    "**/prd*.json",
    "*.contract*",
    "**/*.contract*",
    "*contract*.md",
    "**/*contract*.md",
    "*proposal*.md",
    "**/*proposal*.md",
    "*.proposal*",
    "**/*.proposal*",
)


class MirrorDestinationError(RuntimeError):
    """The mirror destination could not be resolved or proven.

    The message names the variable to set and the value it expected, so an
    operator can act on it directly — rather than a git exit 128 deep inside a
    push.
    """


class DefaultBranchBlockedError(RuntimeError):
    """The mirror's DEFAULT branch was refused and is now a release blocker.

    Carries the full ``Refusal.message``, which names both the paths
    responsible (the substrate that cannot be mirrored) AND the consequence:
    the mirror's default branch is now behind, and every release depending on
    it will be rejected on another forge. This is an alarm, not a log line —
    the fail-visible distinction between "the gate works" (a feature branch
    refused) and "every downstream release is stranded" (the default refused).
    """


@dataclass(frozen=True)
class MirrorDestinationResolution:
    """The outcome of resolving a mirror destination before any push.

    ``destination`` is the ``owner/repo`` to push to. ``source`` records which
    rule produced it: ``"repo override"``, ``"org declaration"``, or
    ``"gated fallback"``. A ``"gated fallback"`` has not yet been proven; call
    :meth:`MirrorHandler.prove_destination` before using it.
    """

    destination: str
    source: str


@dataclass(frozen=True)
class VisibilityDecision:
    """The outcome of classifying one file's visibility (OME-008).

    ``kind`` is ``"product"`` or ``"substrate"``. ``reason`` names the content
    signal that produced the decision, so a blocked push can be audited:
    ``"schema"``, ``"redacted"``, ``"template"``, ``"synthetic"``,
    ``"planning prose"``, or ``"not a planning class"``.
    """

    path: str
    kind: str
    reason: str


@dataclass(frozen=True)
class Refusal:
    """A ref the visibility gate refuses (OME-009).

    ``is_default`` marks the distinction this lane exists for:

    * ``is_default=True``  — a RELEASE BLOCKER. The mirror's default branch is
      now behind, and every release depending on it will be rejected on another
      forge. This must be raised (``DefaultBranchBlockedError``), not logged.
    * ``is_default=False`` — the gate working as intended: substrate on a
      feature/topic branch is refused and nothing downstream is stranded.

    ``substrate`` is the tuple of refused paths, in the ref's tree order.
    """

    ref: str
    is_default: bool
    substrate: tuple[str, ...]

    @property
    def is_release_blocker(self) -> bool:
        return self.is_default

    @property
    def message(self) -> str:
        """A human-readable refusal, distinct for the two cases.

        The default-branch form names BOTH the responsible paths and the
        consequence; the non-default form stays the terse "refused here" the
        gate already produced.
        """
        paths = "\n".join(f"  - {p}" for p in self.substrate)
        if self.is_default:
            return (
                f"HARD GATE: refusing mirror of default branch '{self.ref}' — "
                f"this is a RELEASE BLOCKER.\n"
                f"The mirror's default branch is now BEHIND, and every release "
                f"depending on it will be rejected on another forge.\n"
                f"Responsible paths (substrate that cannot be mirrored):\n{paths}\n"
                f"Break-glass is a deliberate operator action; nothing here "
                f"retries or overrides this refusal."
            )
        return (
            f"mirror gate refuses non-default branch '{self.ref}' "
            f"(the gate working as intended):\n{paths}"
        )


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


    @staticmethod
    def resolve_destination(
        *,
        repo_override: Optional[str] = None,
        org_github_login: Optional[str] = None,
        repo_name: Optional[str] = None,
        fallback: Optional[str] = None,
        variable: str = MIRROR_VARIABLE,
    ) -> MirrorDestinationResolution:
        """Resolve the mirror destination by strict precedence.

        Precedence (see module docstring):

        1. ``repo_override`` (repo-scope ``vars.GH_REPOSITORY``) wins.
        2. ``org_github_login`` composes ``"<login>/<repo_name>"``.
        3. otherwise the run FAILS, naming ``variable`` and the value it
           expected. A ``fallback`` is accepted only as a *gated* candidate
           (``source == "gated fallback"``): it is computed, NOT trusted, and
           must still pass :meth:`prove_destination` before use.

        Raises:
            MirrorDestinationError: no destination is resolvable. The message
                names the variable to set and the value it expected.
        """
        if repo_override:
            return MirrorDestinationResolution(
                destination=repo_override.strip(), source="repo override"
            )

        if org_github_login and repo_name:
            return MirrorDestinationResolution(
                destination=f"{org_github_login.strip()}/{repo_name.strip()}",
                source="org declaration",
            )

        if fallback:
            return MirrorDestinationResolution(
                destination=fallback.strip(), source="gated fallback"
            )

        expected = (
            f"{org_github_login.strip()}/{repo_name.strip()}"
            if org_github_login and repo_name
            else "<github-org>/<repo>"
        )
        raise MirrorDestinationError(
            f"cannot resolve mirror destination: {variable} is unset at both "
            f"repo and org scope, and no org github login is declared to "
            f"compose one. Set {variable} to the destination (owner/repo), "
            f"e.g. {expected}."
        )

    @staticmethod
    async def prove_destination(
        destination: str,
        *,
        token: Optional[str] = None,
        api_base: str = "https://api.github.com",
    ) -> None:
        """Prove the resolved destination with both proofs before any push.

        EXISTS and IS OURS are two independent questions and both must hold:

        * EXISTS  — ``git ls-remote <url> HEAD`` succeeds, i.e. the repository
          is there and readable. This never creates a repository.
        * IS OURS — the repository reply's ``permissions.push`` for the
          authenticated token is true, i.e. we may write to it. Reachability
          alone is not ownership.

        Raises:
            MirrorDestinationError: either proof fails, naming the destination
                and how to fix it. No push has happened and nothing was created.
        """
        remote = f"https://github.com/{destination}.git"
        if token:
            remote = f"https://x-access-token:{token}@github.com/{destination}.git"

        # EXISTS: real git ls-remote, the same proof as the accepted SW152-024
        # preflight. Read-only; a missing repo exits non-zero the same way an
        # unset variable does.
        exists = await MirrorHandler._repo_exists(remote)
        if not exists:
            raise MirrorDestinationError(
                f"cannot vouch for mirror destination '{destination}': it is "
                f"not reachable/readable at {api_base}. Set {MIRROR_VARIABLE} "
                f"to the real destination (owner/repo) and retry."
            )

        is_ours = await MirrorHandler._repo_is_ours(
            destination, token=token, api_base=api_base
        )
        if not is_ours:
            raise MirrorDestinationError(
                f"mirror destination '{destination}' exists but is NOT ours: "
                f"the authenticated token has no push permission on it. The "
                f"name of a public repository owned by someone else must not "
                f"resolve as a writable destination. Correct {MIRROR_VARIABLE} "
                f"to a repository this token may write to."
            )

    @staticmethod
    def classify_visibility(
        path: str,
        *,
        content: Optional[str] = None,
        config: Optional[VisibilityConfig] = None,
    ) -> VisibilityDecision:
        """Classify one file as ``product`` or ``substrate`` by content (OME-008).

        Visibility is a question about what a ref's tree CARRIES, not what a
        push diff touched. A file is inspected only when its path falls into a
        planning class (see ``_VISIBILITY_CANDIDATE_GLOBS``); the verdict within
        that class is content, not name:

        * a JSON schema (``$schema`` + object/properties) is ``product`` — a
          shipped schema must never block;
        * a redacted file (its prose replaced by the neutral placeholder, or by
          the secret-redaction token) is ``product`` — it carries structure, not
          intellectual property;
        * a template or synthetic sample is ``product``;
        * otherwise, a planning-class file whose prose survives is ``substrate``.

        ``content`` is the file's full text. When ``content`` is omitted the
        caller must supply it (a path alone cannot be classified); classification
        without content is refused rather than guessed from the name.
        """
        if content is None:
            raise MirrorDestinationError(
                f"cannot classify visibility of '{path}': content is required. "
                "A name is not intellectual property; only the text can decide "
                "whether a planning-class file carries substrate."
            )

        cfg = config or VisibilityConfig()

        if not MirrorHandler._matches_candidate(path):
            return VisibilityDecision(path=path, kind="product", reason="not a planning class")

        if MirrorHandler._is_json_schema(content):
            return VisibilityDecision(path=path, kind="product", reason="schema")

        if MirrorHandler._is_template(content):
            return VisibilityDecision(path=path, kind="product", reason="template")

        if MirrorHandler._is_synthetic(path, content):
            return VisibilityDecision(path=path, kind="product", reason="synthetic")

        if MirrorHandler._is_redacted(cfg, content):
            return VisibilityDecision(path=path, kind="product", reason="redacted")

        return VisibilityDecision(path=path, kind="substrate", reason="planning prose")

    @staticmethod
    def substrate_files(
        paths: Sequence[str],
        *,
        read: Callable[[str], Optional[str]],
        config: Optional[VisibilityConfig] = None,
    ) -> list[VisibilityDecision]:
        """Classify a ref's full tree and return every substrate file.

        This is the state judgement: the caller supplies the ref's complete
        file inventory (``git ls-tree -r`` on the ref) plus a ``read`` callback
        that yields each file's content at that ref (``git show <ref>:<path>``).
        Files outside a planning class are product and never inspected; every
        planning-class file is classified by content and the substrate ones are
        returned, so a ref whose tree carries planning material is refused even
        when a push diff touched none of it.
        """
        decisions: list[VisibilityDecision] = []
        for path in paths:
            decision = MirrorHandler.classify_visibility(
                path, content=read(path), config=config
            )
            if decision.kind == "substrate":
                decisions.append(decision)
        return decisions

    @staticmethod
    def gate_ref(
        ref: str,
        *,
        default_branch: str = "main",
        paths: Sequence[str],
        read: Callable[[str], Optional[str]],
        config: Optional[VisibilityConfig] = None,
    ) -> Optional["Refusal"]:
        """Refuse a ref whose tree carries substrate (OME-009).

        The gate itself: classifies the ref's full tree (``substrate_files``)
        and returns ``None`` when the ref is clean, or a ``Refusal`` naming
        every refused path. ``is_default`` on the returned ``Refusal`` records
        whether ``ref`` equals ``default_branch`` — the distinction between a
        refused feature branch (the gate working) and a refused default branch
        (a release blocker). This method CLASSIFIES and RETURNS; it does not
        raise and does not push. It never overrides a refusal: the only output
        is a value the caller inspects, and the only "action" any code here can
        take downstream is to raise ``DefaultBranchBlockedError`` via
        :meth:`refuse`. There is no code path that pushes a refused ref.
        """
        substrate = MirrorHandler.substrate_files(paths, read=read, config=config)
        if not substrate:
            return None
        return Refusal(
            ref=ref,
            is_default=(ref == default_branch),
            substrate=tuple(d.path for d in substrate),
        )

    @staticmethod
    def refuse(refusal: Optional["Refusal"]) -> None:
        """Convert a default-branch refusal into visible failure (OME-009).

        The fail-visible step: a ``None`` (clean ref) or a non-default
        ``Refusal`` returns silently so the gate keeps its current behaviour —
        a refused feature branch is logged, not an alarm. A default-branch
        ``Refusal`` raises ``DefaultBranchBlockedError`` whose message names
        the responsible paths AND the consequence. This is the ONLY action this
        lane introduces, and it cannot push any ref: it either returns or
        raises, never writes to a forge.
        """
        if refusal is not None and refusal.is_default:
            raise DefaultBranchBlockedError(refusal.message)

    @staticmethod
    def _matches_candidate(path: str) -> bool:
        """True when ``path`` falls into a planning class (candidate, not verdict)."""
        return any(fnmatch.fnmatch(path, g) for g in _VISIBILITY_CANDIDATE_GLOBS)

    @staticmethod
    def _is_json_schema(content: str) -> bool:
        """A JSON-schema-shaped document (``$schema`` + object properties), not a
        PRD instance. The shipped ``prd.schema.json`` is this shape and must
        never block."""
        return (
            '"$schema"' in content
            and '"type": "object"' in content
            and '"properties"' in content
        )

    @staticmethod
    def _is_template(content: str) -> bool:
        """A template teaches structure; it carries section guidance, not a real
        plan. Detected by structural-guidance markers, not the word 'template'."""
        markers = (
            "## Document Structure",
            "**Purpose:**",
            "**Content:**",
        )
        return sum(1 for m in markers if m in content) >= 2

    @staticmethod
    def _is_synthetic(path: str, content: str) -> bool:
        """A synthetic sample/example declares itself as one rather than naming a
        real work item. Detected by an explicit self-describing marker — in the
        content (``corrected —``, ``a prd in the format``, ``sample``,
        ``example``, ``demo``) or, for a test fixture, in its own name
        (``-sample``, ``-example``). A fixture whose path declares ``sample`` is
        by definition a sample; it carries no planning IP. Never an inferred
        'looks fake' heuristic."""
        head = content[:4096].lower()
        content_markers = (
            "corrected —",
            "a prd in the format",
            "sample",
            "example",
            "demo",
        )
        if any(m in head for m in content_markers):
            return True
        lower_path = path.lower()
        return "-sample" in lower_path or "-example" in lower_path or "sample." in lower_path

    @staticmethod
    def _is_redacted(config: VisibilityConfig, content: str) -> bool:
        """True when the content carries the ecosystem's redaction marker.

        A redacted file has had its prose values replaced by the neutral
        placeholder (SW152-025) or its secrets replaced by the literal
        redaction token. A key dictionary (``lane``, ``criteria``, ``sequence``,
        ``points``) is not IP; the prose is, and a file whose prose is gone is
        ``product`` — structure deliberately retained so it still validates the
        shipped schema. The same path carrying the original prose (unredacted,
        on a tag or non-default branch) is ``substrate``.
        """
        if config.neutral_placeholder:
            marker = config.neutral_placeholder.strip()
            if marker and marker in content:
                return True
        if config.secret_redaction_token:
            token = config.secret_redaction_token.strip()
            if token and token in content:
                return True
        return False

    @staticmethod
    async def _repo_exists(remote: str) -> bool:
        """EXISTS proof: ``git ls-remote <url> HEAD`` exits 0 iff readable."""
        proc = await asyncio.create_subprocess_exec(
            "git",
            "ls-remote",
            remote,
            "HEAD",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await proc.wait() == 0

    @staticmethod
    async def _repo_is_ours(
        destination: str,
        *,
        token: Optional[str],
        api_base: str,
    ) -> bool:
        """IS OURS proof: the repository's permissions.push must be true.

        Uses the forge's REST API (read-only GET). ``permissions.push`` is the
        field that distinguishes "reachable and readable" from "we may write".
        A public repo owned by someone else returns ``permissions.push ==
        false`` (or the request is rejected) and fails this proof.
        """
        import httpx

        headers: dict[str, str] = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"token {token}"
        try:
            async with httpx.AsyncClient(base_url=api_base, timeout=15.0) as client:
                resp = await client.get(f"/repos/{destination}", headers=headers)
        except httpx.HTTPError:
            return False
        if resp.status_code != 200:
            return False
        try:
            permissions = resp.json().get("permissions", {}) or {}
        except ValueError:
            return False
        return bool(permissions.get("push", False))


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
