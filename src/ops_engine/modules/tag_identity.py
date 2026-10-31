"""OE-008 - the mirror tag is the canonical tag object, pushed and verified.

One function, :func:`push_canonical_tag`, closes the mirror-tag race at its
source. It does four things, in this order, and refuses by name at each step:

1. read the CANONICAL tag's object and peel from the canonical forge with the
   credential it is given (``git ls-remote``; the workspace cannot answer this,
   because a checkout re-fetches an annotated tag as ``+<commit>:refs/tags/X``
   and leaves a lightweight local ref);
2. restore the local ref to that object (``git update-ref``), so nothing
   degraded is ever pushed;
3. push the OBJECT (``<sha>:refs/tags/X``, never the ref name) to the mirror;
4. read the mirror back and compare object AND peel.

An annotated canonical tag therefore arrives as the same annotated object on
the mirror, never as a lightweight tag; a lightweight canonical tag stays
lightweight. Git runs through an injectable runner with the convention of
``ops_engine.modules.mirror``: ``await runner(argv, cwd) -> (exit_code,
output)``. The module imports only the standard library, so a release runner
without ``yaml`` and ``pydantic`` can load it by its path. Its name differs from
every name it exports.

The runner may accept an optional ``env`` keyword: ``await runner(argv, cwd,
env=extra)``, where ``extra`` is a dict of environment variables to ADD to the
process environment. It is passed only for a call that carries a credential
(the canonical ``ls-remote``); every other call is ``runner(argv, cwd)``, so a
runner without the keyword keeps working for those. The canonical credential
travels as ``http.extraHeader`` through ``GIT_CONFIG_COUNT`` /
``GIT_CONFIG_KEY_0`` / ``GIT_CONFIG_VALUE_0``: it is in no argv, in no URL and
in no file, and nothing is persisted (no ``remote add``).

The mirror credential is NOT handled here. ``mirror_remote`` is the caller's
remote URL, used exactly as given, as in ``mirror.py``; this function does not
hide, strip or rewrite it. The event path passes a credential-free mirror URL
and relies on git's ambient credentials.

No credential appears in any message: errors name the credential-free URL.
"""

import asyncio
import base64
import os
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

__all__ = [
    "push_canonical_tag",
    "MirrorTagIdentity",
    "MirrorTagIdentityError",
]

Runner = Callable[[list, str], Awaitable[tuple]]

_REF_LINE_RE = re.compile(r"^([0-9a-f]{40})\t(\S+)$")


class MirrorTagIdentityError(RuntimeError):
    """Base of every refusal of :func:`push_canonical_tag`."""


class MissingCanonicalCredentialError(MirrorTagIdentityError):
    """No credential was given for the canonical forge."""


class CanonicalTagUnreadableError(MirrorTagIdentityError):
    """``git ls-remote`` against the canonical forge failed."""


class MissingCanonicalTagError(MirrorTagIdentityError):
    """The tag does not exist on the canonical forge."""


class MissingCanonicalTagObjectError(MirrorTagIdentityError):
    """The canonical tag object is not in the local object store."""


class CanonicalTagUnreachableError(MirrorTagIdentityError):
    """The local copy of the canonical object peels to a different commit."""


class MirrorPushError(MirrorTagIdentityError):
    """``git push`` of the tag object to the mirror failed."""


class MirrorTagMissingError(MirrorTagIdentityError):
    """The mirror holds no such tag after the push."""


class MirrorTagObjectMismatchError(MirrorTagIdentityError):
    """The mirror tag is a different object (annotated vs lightweight drift)."""


class MirrorTagPeelMismatchError(MirrorTagIdentityError):
    """The mirror tag peels to a different commit than the canonical tag."""


@dataclass(frozen=True)
class MirrorTagIdentity:
    """The verified identity of a tag: ``object`` is the tag object (annotated)
    or the commit (lightweight); ``peel`` is always the commit."""

    tag_name: str
    object: str
    peel: str
    annotated: bool


async def _run_git(argv: list, cwd: str, env: Optional[dict] = None) -> tuple:
    """Run git for real; return ``(exit_code, output)`` (stdout and stderr merged).
    ``env`` is added to the process environment."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env={**os.environ, **env} if env else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, (out or b"").decode(errors="replace")


def _redact(url: str) -> str:
    """The URL without userinfo: the only form that is ever put in a message."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    authority, slash, tail = rest.partition("/")
    return "%s://%s%s%s" % (scheme, authority.rpartition("@")[2], slash, tail)


def _credential_env(url: str, token: str) -> dict:
    """The environment that hands the credential to one git invocation as an
    ``Authorization`` header. Only an http(s) URL is authenticated; for a local
    path or other scheme the result is empty."""
    if url.partition("://")[0] not in ("http", "https"):
        return {}
    basic = base64.b64encode(("x-access-token:%s" % token).encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": "Authorization: Basic %s" % basic,
        "GIT_TERMINAL_PROMPT": "0",
    }


def _parse_refs(output: str) -> dict:
    refs = {}
    for line in output.splitlines():
        m = _REF_LINE_RE.match(line.strip())
        if m:
            refs[m.group(2)] = m.group(1)
    return refs


async def _ls_remote(
    run: Runner, remote: str, tag_name: str, cwd: str, env: Optional[dict] = None
) -> tuple:
    """``(exit_code, object, peeled)`` for the tag at ``remote``; ``peeled`` is
    empty for a lightweight tag (git prints no peel line for it)."""
    argv = [
        "git",
        "ls-remote",
        remote,
        "refs/tags/%s" % tag_name,
        "refs/tags/%s^{}" % tag_name,
    ]
    code, out = await (run(argv, cwd, env=env) if env else run(argv, cwd))
    refs = _parse_refs(out) if code == 0 else {}
    return code, refs.get("refs/tags/%s" % tag_name, ""), refs.get(
        "refs/tags/%s^{}" % tag_name, ""
    )


async def push_canonical_tag(
    repo_dir: str,
    tag_name: str,
    canonical_url: str,
    canonical_token: str,
    mirror_remote: str,
    runner: Optional[Runner] = None,
) -> MirrorTagIdentity:
    """Push the canonical tag object to the mirror and verify object and peel.

    ``repo_dir`` is the local clone (it must hold the canonical tag object; a
    checkout that degraded the ref still does). ``canonical_url`` is read with
    ``canonical_token`` through the environment, never argv; ``mirror_remote`` is
    used exactly as given (the mirror credential is the caller's remote URL). ``runner`` defaults
    to real git. Returns the verified :class:`MirrorTagIdentity`; every refusal
    is a subclass of :class:`MirrorTagIdentityError` and nothing is pushed
    before the canonical identity is established.
    """
    run = runner or _run_git
    canonical_name = _redact(canonical_url)
    mirror_name = _redact(mirror_remote)

    if not (canonical_token or "").strip():
        raise MissingCanonicalCredentialError(
            "MissingCanonicalCredentialError: no credential for the canonical "
            "forge %s; refusing to fall back to the local ref, which a "
            "checkout may have degraded." % canonical_name
        )

    # 1. The canonical identity, read from the forge and never from the clone.
    code, obj, peel = await _ls_remote(
        run,
        canonical_url,
        tag_name,
        repo_dir,
        _credential_env(canonical_url, canonical_token.strip()),
    )
    if code != 0:
        raise CanonicalTagUnreadableError(
            "CanonicalTagUnreadableError: git ls-remote against the canonical "
            "forge %s failed (exit %s); the tag identity of %s could not be "
            "read. Nothing was pushed. git output is withheld because it can "
            "carry the credential." % (canonical_name, code, tag_name)
        )
    if not obj:
        raise MissingCanonicalTagError(
            "MissingCanonicalTagError: tag %s does not exist on the canonical "
            "forge %s; nothing was pushed." % (tag_name, canonical_name)
        )
    annotated = bool(peel)
    if not annotated:
        peel = obj  # lightweight: the tag object IS the commit

    # 2. Restore the local ref to the canonical object.
    code, _ = await run(["git", "cat-file", "-t", obj], repo_dir)
    if code != 0:
        raise MissingCanonicalTagObjectError(
            "MissingCanonicalTagObjectError: canonical object %s for tag %s is "
            "not in the local repository; refusing to push a tag whose identity "
            "cannot be established." % (obj, tag_name)
        )
    code, local_peel = await run(["git", "rev-parse", "%s^{}" % obj], repo_dir)
    if code != 0 or local_peel.strip() != peel:
        raise CanonicalTagUnreachableError(
            "CanonicalTagUnreachableError: canonical object %s for tag %s peels "
            "to %s locally, expected %s; refusing to push an unverified tag "
            "object." % (obj, tag_name, local_peel.strip(), peel)
        )
    code, _ = await run(["git", "update-ref", "refs/tags/%s" % tag_name, obj], repo_dir)
    if code != 0:
        raise MissingCanonicalTagObjectError(
            "MissingCanonicalTagObjectError: could not restore refs/tags/%s to "
            "%s (exit %s)." % (tag_name, obj, code)
        )

    # 3. Push the OBJECT, not the ref name.
    code, _ = await run(
        ["git", "push", "--force", mirror_remote, "%s:refs/tags/%s" % (obj, tag_name)],
        repo_dir,
    )
    if code != 0:
        raise MirrorPushError(
            "MirrorPushError: git push of tag %s (object %s) to the mirror %s "
            "failed (exit %s). git output is withheld because it can carry the "
            "credential." % (tag_name, obj, mirror_name, code)
        )

    # 4. Read the mirror back: object first, then peel.
    code, mirror_obj, mirror_peel = await _ls_remote(run, mirror_remote, tag_name, repo_dir)
    if code != 0 or not mirror_obj:
        raise MirrorTagMissingError(
            "MirrorTagMissingError: tag %s has no ref on the mirror %s after the "
            "push. Refusing." % (tag_name, mirror_name)
        )
    if mirror_obj != obj:
        raise MirrorTagObjectMismatchError(
            "MirrorTagObjectMismatchError: tag %s is object %s on the canonical "
            "forge but object %s on the mirror; the mirror tag is not the same "
            "tag object (annotated vs lightweight drift). Refusing."
            % (tag_name, obj, mirror_obj)
        )
    if (mirror_peel or mirror_obj) != peel:
        raise MirrorTagPeelMismatchError(
            "MirrorTagPeelMismatchError: tag %s peels to %s on the canonical "
            "forge but %s on the mirror. Refusing."
            % (tag_name, peel, mirror_peel or mirror_obj)
        )

    return MirrorTagIdentity(tag_name=tag_name, object=obj, peel=peel, annotated=annotated)
