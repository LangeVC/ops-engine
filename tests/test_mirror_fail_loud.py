"""OE-036: the sweep protects the real default branch, stderr is never parsed as
ls-remote output (criteria 1, 2), and a failed sync is red while an ahead
mirror keeps its commits unless the destination opts in (criterion 3)."""

import asyncio
import os
import stat

from ops_engine.config_loader import Destination
from ops_engine.modules.mirror import MirrorHandler

CANON = "https://forgejo.example/langevc/ops-engine.git"
GH = "https://github.com/LangeVC/ops-engine.git"


def _dest():
    return Destination(forge="github", repo="LangeVC/ops-engine", role="mirror", visibility="private")


class _Runner:
    def __init__(self, head, canonical_refs, mirror_refs):
        self.calls = []
        self.head = head
        self.canonical_refs = canonical_refs
        self.mirror_refs = mirror_refs

    async def __call__(self, argv, cwd):
        self.calls.append(list(argv))
        if argv[1] == "symbolic-ref":
            return 0, self.head + "\n"
        if argv[1] == "for-each-ref":
            return 0, "\n".join(self.canonical_refs) + "\n"
        if argv[1] == "ls-remote":
            return 0, "\n".join(f"{'a' * 40}\t{r}" for r in self.mirror_refs) + "\n"
        return 0, ""

    def deletes(self):
        return [a[3] for a in self.calls if a[1] == "push"]


def test_master_default_branch_of_the_canonical_clone_is_kept():
    runner = _Runner(
        head="refs/heads/master",
        canonical_refs=["refs/heads/other"],
        mirror_refs=["refs/heads/master", "refs/heads/stale"],
    )
    lines = asyncio.run(MirrorHandler.sweep_deleted_refs(CANON, [_dest()], runner=runner))
    assert runner.deletes() == [":refs/heads/stale"]
    assert any(l.startswith("mirror-ref-kept ref=refs/heads/master") for l in lines)


def test_unreadable_default_branch_deletes_nothing():
    class Unreadable(_Runner):
        async def __call__(self, argv, cwd):
            if argv[1] == "symbolic-ref":
                self.calls.append(list(argv))
                return 128, ""
            return await super().__call__(argv, cwd)

    runner = Unreadable("", ["refs/heads/other"], ["refs/heads/master", "refs/heads/stale"])
    lines = asyncio.run(MirrorHandler.sweep_deleted_refs(CANON, [_dest()], runner=runner))
    assert runner.deletes() == []
    assert any("mirror-sweep-aborted reason=canonical-head-unreadable" in l for l in lines)


def test_two_word_stderr_line_is_not_read_as_a_ref(tmp_path, monkeypatch):
    log = tmp_path / "pushes.log"
    fake = tmp_path / "bin" / "git"
    fake.parent.mkdir()
    fake.write_text(
        "#!/bin/bash\n"
        'case "$1" in\n'
        '  clone) mkdir -p "$4" ;;\n'
        '  symbolic-ref) echo refs/heads/main ;;\n'
        '  for-each-ref) echo refs/heads/main ;;\n'
        '  ls-remote) echo "warning: redirecting" >&2; echo "' + "a" * 40 + '\trefs/heads/main" ;;\n'
        f'  push) echo "$@" >> "{log}" ;;\n'
        "esac\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    asyncio.run(MirrorHandler.sweep_deleted_refs(CANON, [_dest()], workdir=str(tmp_path)))
    assert not log.exists(), log.read_text()


# --- criterion 3: failed is red, ahead is kept ---------------------------------

import pytest

from ops_engine.modules.reconcile import ReconcileHandler

CSHA = "c" * 40  # canonical tip
MSHA = "d" * 40  # mirror tip (descends from CSHA when ahead)


class _Forge:
    def __init__(self, branches):
        self.branches = dict(branches)

    async def list_branches(self, repo):
        return dict(self.branches)

    async def list_tags(self, repo):
        return {}


class _SyncRunner:
    """Fake git: canonical main=CSHA, mirror main=MSHA, mirror ahead or diverged."""

    def __init__(self, mirror, ahead, clone_code=0, push_code=0):
        self.mirror, self.ahead = mirror, ahead
        self.clone_code, self.push_code = clone_code, push_code
        self.calls = []

    async def __call__(self, argv, cwd):
        self.calls.append(list(argv))
        verb = argv[1]
        if verb == "clone":
            return self.clone_code, ""
        if verb == "ls-remote":
            return 0, f"{MSHA}\trefs/heads/main\n"
        if verb == "for-each-ref":
            if "%(refname) %(objectname)" in argv[2]:
                return 0, f"refs/heads/main {CSHA}\n"
            return 0, "refs/heads/main\n"
        if verb == "merge-base":
            return (0 if self.ahead else 1), ""
        if verb == "push":
            touches_main = any(
                a in ("+refs/heads/*:refs/heads/*", "+refs/heads/main:refs/heads/main")
                for a in argv[3:]
            )
            if self.push_code == 0 and touches_main:
                self.mirror.branches["main"] = CSHA
            return self.push_code, "rejected"
        return 0, ""

    def pushes(self):
        return [a for a in self.calls if a[1] == "push"]


def _cycle(runner, mirror, dest):
    primary = _Forge({"main": CSHA})
    audit = []
    rep = asyncio.run(
        ReconcileHandler.reconcile_mirror(
            primary, "o/r", mirror, "LangeVC/ops-engine", CANON, [dest],
            runner=runner, audit=audit,
        )
    )
    return rep, audit


def test_ahead_mirror_keeps_its_commits_by_default():
    mirror = _Forge({"main": MSHA})
    runner = _SyncRunner(mirror, ahead=True)
    rep, audit = _cycle(runner, mirror, _dest())
    assert mirror.branches["main"] == MSHA
    assert all("+refs/heads/main:refs/heads/main" not in a and "+refs/heads/*:refs/heads/*" not in a
               for a in runner.pushes())
    assert any(l.startswith("mirror-ahead-kept ref=refs/heads/main") for l in audit)
    assert rep.ahead and not rep.behind and not rep.green


def test_ahead_mirror_is_overwritten_only_with_the_explicit_option():
    assert Destination(forge="github", repo="o/r").overwrite_ahead is False
    mirror = _Forge({"main": MSHA})
    runner = _SyncRunner(mirror, ahead=True)
    dest = _dest().model_copy(update={"overwrite_ahead": True})
    rep, _audit = _cycle(runner, mirror, dest)
    assert mirror.branches["main"] == CSHA
    assert len(runner.pushes()) == 1
    assert rep.green


def test_diverged_mirror_is_still_overwritten_by_default():
    mirror = _Forge({"main": MSHA})
    runner = _SyncRunner(mirror, ahead=False)
    rep, _audit = _cycle(runner, mirror, _dest())
    assert mirror.branches["main"] == CSHA
    assert rep.green


def test_failed_push_makes_the_result_red_and_distinct_from_behind():
    mirror = _Forge({"main": MSHA})
    runner = _SyncRunner(mirror, ahead=False, push_code=1)
    rep, _audit = _cycle(runner, mirror, _dest())
    assert rep.failed and rep.behind
    assert not rep.green
    assert rep.to_dict()["failed"] and rep.to_dict()["green"] is False


def test_failed_canonical_clone_makes_the_result_red():
    mirror = _Forge({"main": "0" * 40})
    runner = _SyncRunner(mirror, ahead=False, clone_code=128)
    rep, audit = _cycle(runner, mirror, _dest())
    assert runner.pushes() == []
    assert any("canonical-clone-failed" in l for l in audit)
    assert rep.failed and not rep.green
