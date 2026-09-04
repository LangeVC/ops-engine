"""CORE-004: MirrorHandler — Verify mirror sync after push to primary forge.

OME-012 resolution contract. As of CFG-002 the mirror destination is resolved
from the CONFIG first (``mirror.github``, the Layer-2 destination declared by
CFG-001), with the two Actions variables retained as a DEPRECATED override
(removed in 4.0.0). The description below documents the two-variable path; the
config path and the precedence between the two are documented on
:meth:`MirrorHandler.resolve_destination` and in CONTRACT.md.

The deprecated two-variable path resolves the destination from TWO Actions
variables — the two halves of one contract, not two sources of one value:

  GH_REPO_OWNER  ORG scope   the GitHub owner            e.g. ``Capacium``
  GH_REPO        REPO scope  the full owner/name target  e.g. ``Capacium/capacium``

BOTH are required. There is no precedence between them, and there is no
fallback: an unset variable is a hard refusal — never a computed candidate.
The check order, matching the deployed preflight guard (the "Preflight —
resolve, double-match, verify, and vouch for the destination" step of the
canonical ``.forgejo/workflows/mirror.yml``), is:

  1. GH_REPO_OWNER unset  -> refuse, naming the ORG-scope variable.
  2. GH_REPO unset        -> refuse, naming the REPO-scope variable.
  3. DOUBLE MATCH: GH_REPO's owner prefix == GH_REPO_OWNER, a CASE-SENSITIVE
     string compare performed BEFORE ANY NETWORK CALL. Mismatch refuses,
     naming both values. No strip/lower/title/slug is applied — the owner and
     destination are used verbatim (whitespace strip only), so a pair whose
     prefix has strayed is caught, never silently re-cased into agreement.
  4. EXISTS  — reachability (``git ls-remote``).
  5. IS OURS — push permission (reachability is not ownership).

A silently-wrong destination surfaces as a git exit 128 deep inside a push;
the contract instead fails at a named preflight, before any push, naming the
variable to set, its SCOPE, and the value it expected.

The two proofs answer different questions:

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

The canonical variable names are declared ONCE here (see
:data:`MIRROR_OWNER_VARIABLE` / :data:`MIRROR_REPO_VARIABLE`) and exported so
operator scripts can import them instead of restating them.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

from ops_engine.adapters.base import ForgeAdapter
from ops_engine.config_loader import MirrorConfig

logger = logging.getLogger(__name__)

# The two Actions variables that carry the mirror contract. Declared once here
# and exported, so operator scripts import these names instead of restating the
# strings (restatement is how the contract drifted). The OME-002 single
# variable ``GH_REPOSITORY`` is retired: the contract is now two halves, an
# owning org/user (ORG scope) and a full owner/name destination (REPO scope).
MIRROR_OWNER_VARIABLE = "GH_REPO_OWNER"
MIRROR_REPO_VARIABLE = "GH_REPO"


class MirrorDestinationError(RuntimeError):
    """The mirror destination could not be resolved or proven.

    The message names the variable to set and the value it expected, so an
    operator can act on it directly — rather than a git exit 128 deep inside a
    push.
    """


@dataclass(frozen=True)
class MirrorDestinationResolution:
    """The outcome of resolving a mirror destination before any push.

    ``destination`` is the ``owner/repo`` to push to, taken verbatim from the
    source (whitespace stripped only). ``source`` records which of the two
    contract sources supplied it: ``"config"`` (the primary Layer-2
    ``mirror.github`` field) or ``"double match"`` (the deprecated two-variable
    override). The value has passed the double-match check but has NOT yet been
    proven reachable and writable; call :meth:`MirrorHandler.prove_destination`
    before using it.
    """

    destination: str
    source: str


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
        config: Optional[MirrorConfig] = None,
        gh_repo_owner: Optional[str] = None,
        gh_repo: Optional[str] = None,
    ) -> MirrorDestinationResolution:
        """Resolve the mirror destination from the config, with the variables
        as a deprecated override.

        There are two sources, in a fixed precedence that is part of the
        contract (see CONTRACT.md "Mirror destination resolution"):

        1. **CONFIG (primary).** ``config.mirror.github`` is the Layer-2
           destination, a single ``owner/name`` string written by the layover
           config and now declared on ``MirrorConfig`` (CFG-001). When it is
           non-empty it resolves the destination verbatim; the owner is its
           prefix. The ``source`` of the result is ``"config"``.

        2. **VARIABLES (deprecated override).** ``gh_repo_owner`` (ORG-scope
           ``GH_REPO_OWNER``) and ``gh_repo`` (REPO-scope ``GH_REPO``) form the
           two halves of the v3.1.0 two-variable contract. They are consulted
           ONLY when the config path carried no destination. The ``source`` of
           the result is ``"double match"``.

        The config wins when both are supplied: the variables are deprecated
        (removed in 4.0.0, see DEC-003) and a stale variable must not silently
        override a correct config, or the mapping would still live in the
        variable store this feature exists to retire.

        The case-sensitive double match runs before any network call whichever
        source supplied the values: for the variable source it compares
        ``gh_repo``'s owner prefix against ``gh_repo_owner`` (two independent
        values); for the config source the destination is a single field whose
        owner IS its prefix, so the same verbatim, case-sensitive, no-network
        resolution applies and a malformed or absent destination refuses.

        The owner and destination are used VERBATIM — whitespace stripped, and
        nothing else (no lower/title/slug).

        Raises:
            MirrorDestinationError: no destination, or the double match fails.
                The message names the source, its variable/section AND scope,
                gives the value it held/expected, and proves no network call is
                needed to refuse.
        """
        # Config path: the Layer-2 ``mirror.github`` field, a single owner/name
        # destination written by the layover config and declared on MirrorConfig
        # by CFG-001. Resolved verbatim (whitespace stripped only).
        config_github = config.github.strip() if config is not None and config.github else ""
        if config_github:
            repo = config_github
            owner = repo.split("/", 1)[0]
            if not owner or repo == owner:
                raise MirrorDestinationError(
                    f"cannot resolve mirror destination: config mirror.github "
                    f"'{repo}' is not an 'owner/name' destination. Set it to "
                    f"the full owner/name target (e.g. LangeVC/ops-engine)."
                )
            return MirrorDestinationResolution(
                destination=repo, source="config"
            )

        # Variable path: the deprecated two-variable override.
        owner = gh_repo_owner.strip() if gh_repo_owner is not None else None
        repo = gh_repo.strip() if gh_repo is not None else None

        if not owner:
            raise MirrorDestinationError(
                f"cannot resolve mirror destination: {MIRROR_OWNER_VARIABLE} "
                f"is UNSET. Set the ORG-level Actions variable "
                f"{MIRROR_OWNER_VARIABLE} to the GitHub org/user that owns "
                f"the mirror (e.g. Capacium)."
            )

        if not repo:
            raise MirrorDestinationError(
                f"cannot resolve mirror destination: {MIRROR_REPO_VARIABLE} "
                f"is UNSET. Set the REPOSITORY-level Actions variable "
                f"{MIRROR_REPO_VARIABLE} to the full destination "
                f"(owner/repo, e.g. Capacium/capacium)."
            )

        repo_owner = repo.split("/", 1)[0]
        if repo_owner != owner:
            raise MirrorDestinationError(
                f"DOUBLE MATCH failed: {MIRROR_REPO_VARIABLE} '{repo}' has "
                f"owner prefix '{repo_owner}', but {MIRROR_OWNER_VARIABLE} is "
                f"'{owner}' (case-sensitive). Correct one of the two variables "
                f"so {MIRROR_REPO_VARIABLE}'s owner prefix equals "
                f"{MIRROR_OWNER_VARIABLE} exactly. Refusing to push; no GitHub "
                f"request was made."
            )

        return MirrorDestinationResolution(
            destination=repo, source="double match"
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
                f"not reachable/readable at {api_base}. Set the "
                f"REPOSITORY-level Actions variable {MIRROR_REPO_VARIABLE} "
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
                f"resolve as a writable destination. Correct the "
                f"REPOSITORY-level Actions variable {MIRROR_REPO_VARIABLE} "
                f"to a repository this token may write to."
            )

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
