"""OE-008 criteria 1 and 2: the mirror tag is the canonical tag object.

Real local bare repositories stand in for the canonical forge and the mirror;
no test touches the network. The workspace ref is deliberately degraded to a
lightweight tag first, which is what actions/checkout leaves behind for an
annotated tag.
"""

import asyncio
import subprocess

import pytest

from ops_engine import MirrorTagIdentity, MirrorTagIdentityError, push_canonical_tag
from ops_engine.modules import tag_identity

TAG = "v1.2.3"
TOKEN = "s3cr3t-forge-token"


def git(*argv, cwd=None):
    return subprocess.run(
        ["git", *argv], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _ident(repo):
    git("config", "user.email", "t@example.invalid", cwd=repo)
    git("config", "user.name", "T", cwd=repo)


@pytest.fixture
def world(tmp_path):
    canonical = tmp_path / "canonical.git"
    mirror = tmp_path / "mirror.git"
    work = tmp_path / "work"
    git("init", "--bare", "-q", str(canonical))
    git("init", "--bare", "-q", str(mirror))
    git("init", "-q", str(work))
    _ident(work)
    (work / "f.txt").write_text("one\n")
    git("add", "f.txt", cwd=work)
    git("commit", "-q", "-m", "one", cwd=work)
    commit = git("rev-parse", "HEAD", cwd=work)
    return {
        "canonical": str(canonical),
        "mirror": str(mirror),
        "work": str(work),
        "commit": commit,
    }


def _publish_canonical(world, annotated):
    work = world["work"]
    if annotated:
        git("tag", "-a", TAG, "-m", "release", cwd=work)
    else:
        git("tag", TAG, cwd=work)
    git("push", "-q", world["canonical"], f"refs/tags/{TAG}", cwd=work)
    # Degrade the workspace ref the way actions/checkout does: the tag object
    # stays in the object store, the ref is re-pointed at the bare commit.
    git("update-ref", f"refs/tags/{TAG}", world["commit"], cwd=work)


def _push(world, **kw):
    args = dict(
        repo_dir=world["work"],
        tag_name=TAG,
        canonical_url=world["canonical"],
        canonical_token=TOKEN,
        mirror_remote=world["mirror"],
    )
    args.update(kw)
    return asyncio.run(push_canonical_tag(**args))


def test_annotated_canonical_tag_becomes_the_same_annotated_object_on_the_mirror(world):
    _publish_canonical(world, annotated=True)
    canonical_obj = git("rev-parse", f"refs/tags/{TAG}", cwd=world["canonical"])
    assert canonical_obj != world["commit"]  # it really is a tag object

    result = _push(world)

    assert isinstance(result, MirrorTagIdentity)
    assert result.object == canonical_obj
    assert result.peel == world["commit"]
    assert result.annotated is True
    mirror = world["mirror"]
    assert git("rev-parse", f"refs/tags/{TAG}", cwd=mirror) == canonical_obj
    assert git("cat-file", "-t", f"refs/tags/{TAG}", cwd=mirror) == "tag"
    assert git("rev-parse", f"refs/tags/{TAG}^{{}}", cwd=mirror) == world["commit"]


def test_local_ref_is_restored_to_the_canonical_object(world):
    _publish_canonical(world, annotated=True)
    assert git("cat-file", "-t", f"refs/tags/{TAG}", cwd=world["work"]) == "commit"
    result = _push(world)
    assert git("rev-parse", f"refs/tags/{TAG}", cwd=world["work"]) == result.object
    assert git("cat-file", "-t", f"refs/tags/{TAG}", cwd=world["work"]) == "tag"


def test_lightweight_canonical_tag_stays_lightweight_on_the_mirror(world):
    _publish_canonical(world, annotated=False)
    result = _push(world)
    assert result.annotated is False
    assert result.object == result.peel == world["commit"]
    assert git("cat-file", "-t", f"refs/tags/{TAG}", cwd=world["mirror"]) == "commit"


def test_a_lightweight_mirror_tag_is_replaced_by_the_annotated_object(world):
    _publish_canonical(world, annotated=True)
    git("push", "-q", world["mirror"], f"{world['commit']}:refs/tags/{TAG}", cwd=world["work"])
    assert git("cat-file", "-t", f"refs/tags/{TAG}", cwd=world["mirror"]) == "commit"
    result = _push(world)
    assert git("rev-parse", f"refs/tags/{TAG}", cwd=world["mirror"]) == result.object
    assert git("cat-file", "-t", f"refs/tags/{TAG}", cwd=world["mirror"]) == "tag"


def test_empty_credential_is_refused_before_anything_is_read_or_pushed(world):
    _publish_canonical(world, annotated=True)
    calls = []

    async def runner(argv, cwd):
        calls.append(argv)
        return 0, ""

    with pytest.raises(tag_identity.MissingCanonicalCredentialError):
        _push(world, canonical_token="  ", runner=runner)
    assert calls == []


def test_missing_canonical_tag_is_refused_by_name_and_pushes_nothing(world):
    with pytest.raises(tag_identity.MissingCanonicalTagError):
        _push(world)
    assert git("tag", "-l", cwd=world["mirror"]) == ""


def test_unreadable_canonical_forge_is_refused_by_name(world, tmp_path):
    with pytest.raises(tag_identity.CanonicalTagUnreadableError):
        _push(world, canonical_url=str(tmp_path / "nowhere.git"))


def test_canonical_object_absent_from_the_workspace_is_refused(world, tmp_path):
    _publish_canonical(world, annotated=True)
    empty = tmp_path / "empty"
    git("init", "-q", str(empty))
    with pytest.raises(tag_identity.MissingCanonicalTagObjectError):
        _push(world, repo_dir=str(empty))
    assert git("tag", "-l", cwd=world["mirror"]) == ""


def test_mirror_holding_a_different_object_is_refused_by_name(world):
    _publish_canonical(world, annotated=True)

    async def lightweight_push(argv, cwd):
        if argv[:2] == ["git", "push"]:
            argv = [*argv[:-1], f"{world['commit']}:refs/tags/{TAG}"]
        return await tag_identity._run_git(argv, cwd)

    with pytest.raises(tag_identity.MirrorTagObjectMismatchError):
        _push(world, runner=lightweight_push)


def test_missing_mirror_tag_after_the_push_is_refused_by_name(world):
    _publish_canonical(world, annotated=True)

    async def swallowed_push(argv, cwd):
        if argv[:2] == ["git", "push"]:
            return 0, ""
        return await tag_identity._run_git(argv, cwd)

    with pytest.raises(tag_identity.MirrorTagMissingError):
        _push(world, runner=swallowed_push)


def test_every_refusal_is_a_mirror_tag_identity_error():
    for name in (
        "MissingCanonicalCredentialError",
        "CanonicalTagUnreadableError",
        "MissingCanonicalTagError",
        "MissingCanonicalTagObjectError",
        "CanonicalTagUnreachableError",
        "MirrorPushError",
        "MirrorTagMissingError",
        "MirrorTagObjectMismatchError",
        "MirrorTagPeelMismatchError",
    ):
        assert issubclass(getattr(tag_identity, name), MirrorTagIdentityError)


def test_credential_goes_to_git_only_for_the_canonical_read_and_never_into_a_message():
    calls = []
    canon = "https://forge.example.invalid/org/repo.git"

    async def runner(argv, cwd):
        calls.append(argv)
        if argv[1] == "ls-remote":
            return 128, f"fatal: Authentication failed for '{argv[2]}'"
        return 0, ""

    with pytest.raises(tag_identity.CanonicalTagUnreadableError) as err:
        asyncio.run(
            push_canonical_tag(
                repo_dir="/w",
                tag_name=TAG,
                canonical_url=canon,
                canonical_token=TOKEN,
                mirror_remote="https://mirror.example.invalid/o/r.git",
                runner=runner,
            )
        )
    assert calls[0][:2] == ["git", "ls-remote"]
    assert TOKEN in calls[0][2]  # handed to git for the one canonical read
    assert TOKEN not in str(err.value)
    assert canon in str(err.value)  # the credential-free URL is what is named
    assert len(calls) == 1  # nothing was pushed
