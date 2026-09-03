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
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

from ops_engine.adapters.base import ForgeAdapter
from ops_engine.config_loader import MirrorConfig

logger = logging.getLogger(__name__)

# The repository variable (repo or org scope) whose value, when set, is the
# per-repository override. Repo scope wins over org scope (measured: run 38407 /
# job 68398, a PLATFORM rule on this Forgejo instance, not a repo property).
MIRROR_VARIABLE = "GH_REPOSITORY"


class MirrorDestinationError(RuntimeError):
    """The mirror destination could not be resolved or proven.

    The message names the variable to set and the value it expected, so an
    operator can act on it directly — rather than a git exit 128 deep inside a
    push.
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
