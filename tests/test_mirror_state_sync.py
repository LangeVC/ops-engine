"""OE-023 criteria 1 and 2: full state sync and the on-request deletion sweep.

State sync pushes every canonical head and tag to every mirror destination
(force, no prune) and writes nothing unless a fresh clone of the canonical
repository succeeded. The deletion sweep removes mirror refs absent on the
canonical clone, only when called, and never the default branch, ``dev`` or
GitHub-managed refs. Fake-runner tests assert exact argv; real-git tests use
local bare repos with the forge URLs rewritten (no network).
"""

import asyncio
import os
import subprocess

import pytest

from ops_engine.config_loader import Destination
from ops_engine.modules.mirror import MirrorHandler

CANON = "https://forgejo.example/langevc/ops-engine.git"


def _dests():
    return [
        Destination(forge="github", repo="LangeVC/ops-engine", role="mirror", visibility="private"),
        Destination(forge="gitlab", repo="langevc/ops-engine", role="mirror", visibility="private"),
        Destination(forge="github", repo="LangeVC/ops-engine-rel", role="release"),
    ]


class Runner:
    def __init__(self, clone_code=0, canonical_refs=(), mirror_refs=None, head="refs/heads/main"):
        self.calls = []
        self.head = head
        self.clone_code = clone_code
        self.canonical_refs = list(canonical_refs)
        self.mirror_refs = mirror_refs or {}

    async def __call__(self, argv, cwd):
        self.calls.append((list(argv), cwd))
        if argv[1] == "clone":
            return self.clone_code, ""
        if argv[1] == "for-each-ref":
            return 0, "\n".join(self.canonical_refs) + "\n"
        if argv[1] == "symbolic-ref":
            return 0, self.head + "\n"
        if argv[1] == "ls-remote":
            rows = [f"{'a' * 40}\t{r}" for r in self.mirror_refs.get(argv[-1], [])]
            return 0, "\n".join(rows) + "\n"
        return 0, ""

    def verbs(self):
        return [argv[1] for argv, _ in self.calls]


def _sync(runner, audit=None):
    return asyncio.run(
        MirrorHandler.sync_state(CANON, _dests(), runner=runner, audit=audit)
    )


def _sweep(runner, audit=None, **kw):
    return asyncio.run(
        MirrorHandler.sweep_deleted_refs(
            CANON, _dests(), runner=runner, audit=audit, **kw
        )
    )


# -- criterion 1 -------------------------------------------------------------


def test_sync_clones_fresh_then_pushes_all_heads_and_tags_to_each_mirror():
    runner, audit = Runner(), []
    lines = _sync(runner, audit)
    # release destination skipped; each mirror is probed read-only (OE-036) before its push
    assert runner.verbs() == [
        "clone", "ls-remote", "for-each-ref", "push", "ls-remote", "for-each-ref", "push",
    ]
    clone_argv, _ = runner.calls[0]
    assert clone_argv[:3] == ["git", "clone", "--bare"] and clone_argv[3] == CANON
    pushes = [argv for argv, _ in runner.calls if argv[1] == "push"]
    assert pushes[0][:3] == ["git", "push", "https://github.com/LangeVC/ops-engine.git"]
    assert pushes[1][2] == "https://gitlab.com/langevc/ops-engine.git"
    for argv in pushes:
        assert argv[3:] == ["+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"]
        assert not any(a in ("--prune", "--mirror", "--delete") for a in argv)
        assert not any(a.startswith(":") for a in argv)
    # the pushes run inside the fresh clone, not in some other working copy
    assert all(cwd == clone_argv[4] for _, cwd in runner.calls[1:])
    assert not any(a[1] in ("fetch", "merge-base") for a, _ in runner.calls)
    assert len(lines) == 2 and audit == lines
    assert all("exit=0" in l for l in lines)


def test_failed_clone_writes_nothing():
    runner, audit = Runner(clone_code=128), []
    lines = _sync(runner, audit)
    assert runner.verbs() == ["clone"]
    assert len(lines) == 1 and lines[0].startswith("mirror-state-aborted")
    assert "exit=128" in lines[0] and audit == lines


def test_failed_push_is_audited_and_does_not_stop_the_other_destination():
    class Failing(Runner):
        async def __call__(self, argv, cwd):
            code, out = await super().__call__(argv, cwd)
            if argv[1] == "push" and "github.com" in argv[2]:
                return 1, ""
            return code, out

    runner = Failing()
    lines = _sync(runner)
    assert runner.verbs() == [
        "clone", "ls-remote", "for-each-ref", "push", "ls-remote", "for-each-ref", "push",
    ]
    assert "exit=1" in lines[0] and "exit=0" in lines[1]


def test_sync_never_deletes():
    runner = Runner(canonical_refs=[], mirror_refs={})
    _sync(runner)
    # the ahead probe (OE-036) reads the mirror, but nothing is ever deleted
    pushes = [argv for argv, _ in runner.calls if argv[1] == "push"]
    assert pushes and not any(a.startswith(":") or a == "--delete" for p in pushes for a in p[3:])


# -- criterion 2 -------------------------------------------------------------

GH = "https://github.com/LangeVC/ops-engine.git"
GL = "https://gitlab.com/langevc/ops-engine.git"


def test_sweep_deletes_only_refs_absent_on_canonical_and_reports_each():
    runner = Runner(
        canonical_refs=["refs/heads/main", "refs/heads/keep", "refs/tags/v1"],
        mirror_refs={
            GH: ["refs/heads/main", "refs/heads/keep", "refs/heads/gone", "refs/tags/v0"],
            GL: ["refs/heads/main", "refs/heads/keep", "refs/tags/v1"],
        },
    )
    audit = []
    lines = _sweep(runner, audit)
    deletes = [argv for argv, _ in runner.calls if argv[1] == "push"]
    assert deletes == [
        ["git", "push", GH, ":refs/heads/gone"],
        ["git", "push", GH, ":refs/tags/v0"],
    ]
    deleted = [l for l in lines if l.startswith("mirror-ref-deleted")]
    assert len(deleted) == 2 and audit == lines
    assert "ref=refs/heads/gone" in deleted[0] and "dest=github:LangeVC/ops-engine" in deleted[0]
    assert "ref=refs/tags/v0" in deleted[1] and "exit=0" in deleted[1]


def test_sweep_never_deletes_default_dev_or_github_managed_refs():
    protected = [
        "refs/heads/trunk",
        "refs/heads/dev",
        "refs/heads/gh-pages",
        "refs/heads/dependabot/pip/x",
        "refs/heads/copilot/y",
        "refs/pull/7/head",
        "refs/keep-around/abc",
    ]
    runner = Runner(
        canonical_refs=["refs/heads/other"],
        mirror_refs={GH: protected + ["refs/heads/stale"], GL: []},
        head="refs/heads/trunk",
    )
    lines = _sweep(runner)
    deletes = [argv[3] for argv, _ in runner.calls if argv[1] == "push"]
    assert deletes == [":refs/heads/stale"]
    kept = {l.split()[1] for l in lines if l.startswith("mirror-ref-kept")}
    assert kept == {f"ref={r}" for r in protected}


def test_sweep_ignores_peeled_tag_rows():
    class Peeled(Runner):
        async def __call__(self, argv, cwd):
            if argv[1] == "ls-remote" and argv[-1] == GH:
                self.calls.append((list(argv), cwd))
                return 0, f"{'a' * 40}\trefs/tags/v1\n{'b' * 40}\trefs/tags/v1^{{}}\n"
            return await super().__call__(argv, cwd)

    runner = Peeled(canonical_refs=["refs/heads/main", "refs/tags/v1"])
    _sweep(runner)
    assert [a for a, _ in runner.calls if a[1] == "push"] == []


def test_sweep_deletes_nothing_when_the_canonical_clone_fails():
    runner = Runner(clone_code=128, mirror_refs={GH: ["refs/heads/x"]})
    lines = _sweep(runner)
    assert runner.verbs() == ["clone"]
    assert lines[0].startswith("mirror-sweep-aborted")


def test_sweep_deletes_nothing_when_canonical_has_no_refs():
    runner = Runner(canonical_refs=[], mirror_refs={GH: ["refs/heads/x"]})
    lines = _sweep(runner)
    assert "push" not in runner.verbs()
    assert lines[0].startswith("mirror-sweep-aborted")


# -- real git, real runner, local bare repos, no network ---------------------


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _refs(bare):
    out = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)"],
        cwd=bare, check=True, capture_output=True, text=True,
    ).stdout
    return set(out.split())


@pytest.fixture
def world(tmp_path, monkeypatch):
    canon = tmp_path / "canon.git"
    gh = tmp_path / "gh.git"
    gl = tmp_path / "gl.git"
    for bare in (canon, gh, gl):
        _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    work = tmp_path / "work"
    work.mkdir()
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    _git(work, "init", "-q", "-b", "main")
    (work / "a.txt").write_text("a\n")
    _git(work, "add", ".")
    _git(work, *ident, "commit", "-q", "-m", "base")
    _git(work, "branch", "feature")
    _git(work, "tag", "v1")
    _git(work, "push", "-q", str(canon), "main", "feature", "refs/tags/v1")
    rewrites = {
        GH: f"file://{gh}",
        GL: f"file://{gl}",
        "https://forgejo.example/langevc/ops-engine.git": f"file://{canon}",
        "https://forgejo.example/langevc/does-not-exist.git": f"file://{tmp_path}/nope.git",
    }
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(rewrites)))
    for i, (url, local) in enumerate(rewrites.items()):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{i}", f"url.{local}.insteadOf")
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{i}", url)
    return work, canon, gh, gl


def test_real_git_sync_mirrors_every_head_and_tag_then_sweep_removes_stale(world, tmp_path):
    work, canon, gh, gl = world
    lines = asyncio.run(
        MirrorHandler.sync_state(CANON, _dests(), workdir=str(tmp_path))
    )
    assert all("exit=0" in l for l in lines) and len(lines) == 2
    want = {"refs/heads/main", "refs/heads/feature", "refs/tags/v1"}
    assert _refs(gh) == want and _refs(gl) == want

    # a stale branch and tag appear on the mirror; sync alone must not remove them
    _git(work, "branch", "stale")
    _git(work, "tag", "vold")
    _git(work, "branch", "dev")
    _git(work, "push", "-q", f"file://{gh}", "stale", "dev", "refs/tags/vold")
    asyncio.run(MirrorHandler.sync_state(CANON, _dests(), workdir=str(tmp_path)))
    assert {"refs/heads/stale", "refs/tags/vold", "refs/heads/dev"} <= _refs(gh)

    lines = asyncio.run(
        MirrorHandler.sweep_deleted_refs(CANON, _dests(), workdir=str(tmp_path))
    )
    deleted = sorted(l.split()[1] for l in lines if l.startswith("mirror-ref-deleted"))
    assert deleted == ["ref=refs/heads/stale", "ref=refs/tags/vold"]
    assert _refs(gh) == want | {"refs/heads/dev"}  # dev survives
    assert _refs(canon) == want


def test_real_git_unreachable_canonical_writes_nothing(world, tmp_path):
    work, canon, gh, gl = world
    before = _refs(gh), _refs(gl)
    bad = "https://forgejo.example/langevc/does-not-exist.git"
    lines = asyncio.run(MirrorHandler.sync_state(bad, _dests(), workdir=str(tmp_path)))
    assert len(lines) == 1 and lines[0].startswith("mirror-state-aborted")
    assert (_refs(gh), _refs(gl)) == before
    assert os.listdir(tmp_path / "work")  # unrelated sanity: workdir untouched


# -- criterion 3: RED proof, end to end with real git -------------------------


def _all_refs(*bares):
    return tuple(_refs(b) for b in bares)


def test_real_git_branch_deleted_on_canonical_disappears_only_through_the_sweep(
    world, tmp_path
):
    work, canon, gh, gl = world
    wd = str(tmp_path)
    asyncio.run(MirrorHandler.sync_state(CANON, _dests(), workdir=wd))
    assert "refs/heads/feature" in _refs(gh) and "refs/heads/feature" in _refs(gl)

    # the branch is deleted on the canonical (Forgejo) repository
    _git(work, "push", "-q", str(canon), ":refs/heads/feature")
    assert "refs/heads/feature" not in _refs(canon)

    # state sync, even repeated, leaves it on the mirrors
    for _ in range(2):
        asyncio.run(MirrorHandler.sync_state(CANON, _dests(), workdir=wd))
    assert "refs/heads/feature" in _refs(gh) and "refs/heads/feature" in _refs(gl)

    # only the sweep removes it, from every mirror, and nothing else
    lines = asyncio.run(MirrorHandler.sweep_deleted_refs(CANON, _dests(), workdir=wd))
    deleted = [l for l in lines if l.startswith("mirror-ref-deleted")]
    assert len(deleted) == 2 and all("ref=refs/heads/feature" in l for l in deleted)
    assert _refs(gh) == _refs(gl) == {"refs/heads/main", "refs/tags/v1"}


def test_real_git_failing_canonical_clone_makes_neither_path_write(world, tmp_path):
    work, canon, gh, gl = world
    wd = str(tmp_path)
    asyncio.run(MirrorHandler.sync_state(CANON, _dests(), workdir=wd))
    # mirrors now hold a ref that canonical lacks and canonical gained a new one
    _git(work, "branch", "fresh")
    _git(work, "push", "-q", str(canon), "fresh")
    _git(work, "push", "-q", f"file://{gh}", "feature:refs/heads/extra")
    before = _all_refs(gh, gl)
    bad = "https://forgejo.example/langevc/does-not-exist.git"

    synced = asyncio.run(MirrorHandler.sync_state(bad, _dests(), workdir=wd))
    swept = asyncio.run(MirrorHandler.sweep_deleted_refs(bad, _dests(), workdir=wd))

    assert synced[0].startswith("mirror-state-aborted") and len(synced) == 1
    assert swept[0].startswith("mirror-sweep-aborted") and len(swept) == 1
    assert _all_refs(gh, gl) == before
    assert "refs/heads/fresh" not in _refs(gh) and "refs/heads/extra" in _refs(gh)
