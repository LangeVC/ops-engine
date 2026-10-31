"""OE-035 criterion 1: the visibility gate covers the state-sync path.

``sync_state`` must not push a branch that touches a candidate path to a public
destination, must audit the refusal, and must still push that branch to a
private destination in the same sync. Real git, local bare repos, no network.
"""

import asyncio
import os
import subprocess

import pytest

from ops_engine.config_loader import Destination
from ops_engine.modules.mirror import MirrorHandler


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _has_ref(bare, ref):
    return subprocess.run(
        ["git", "rev-parse", "--verify", "-q", ref], cwd=bare, capture_output=True
    ).returncode == 0


def _dests():
    return [
        Destination(forge="github", repo="LangeVC/pub", role="mirror", visibility="public"),
        Destination(forge="gitlab", repo="langevc/priv", role="mirror", visibility="private"),
    ]


@pytest.fixture
def world(tmp_path, monkeypatch):
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "a.txt").write_text("a\n")
    _git(work, "add", ".")
    _git(work, *ident, "commit", "-q", "-m", "base")
    _git(work, "checkout", "-q", "-b", "plan")
    (work / ".skillweave").mkdir()
    (work / ".skillweave" / "prd.json").write_text("{}\n")
    _git(work, "add", ".")
    _git(work, *ident, "commit", "-q", "-m", "plan")
    _git(work, "checkout", "-q", "-b", "clean", "main")
    (work / "b.txt").write_text("b\n")
    _git(work, "add", ".")
    _git(work, *ident, "commit", "-q", "-m", "clean")
    canon, pub, priv = tmp_path / "canon.git", tmp_path / "pub.git", tmp_path / "priv.git"
    for bare in (canon, pub, priv):
        _git(tmp_path, "init", "-q", "--bare", str(bare))
    _git(work, "push", "-q", str(canon), "main", "plan", "clean")
    rewrites = {
        "https://github.com/LangeVC/pub.git": f"file://{pub}",
        "https://gitlab.com/langevc/priv.git": f"file://{priv}",
    }
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(rewrites)))
    for i, (url, local) in enumerate(rewrites.items()):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{i}", f"url.{local}.insteadOf")
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{i}", url)
    return canon, pub, priv


def _sync(canon, audit, **kw):
    return asyncio.run(
        MirrorHandler.sync_state(
            f"file://{canon}", _dests(), audit=audit, **kw
        )
    )


def test_state_sync_refuses_prd_branch_on_public_and_pushes_it_to_private(world):
    canon, pub, priv = world
    audit = []
    lines = _sync(canon, audit)
    # RED against the pre-change code: pub received refs/heads/plan.
    assert not _has_ref(pub, "refs/heads/plan")
    assert _has_ref(priv, "refs/heads/plan")
    refused = [l for l in lines if l.startswith("mirror-refused")]
    assert len(refused) == 1
    assert "ref=refs/heads/plan" in refused[0]
    assert "dest=github:LangeVC/pub" in refused[0]
    assert ".skillweave/prd.json" in refused[0]
    assert audit == lines


def test_state_sync_still_delivers_clean_branches_to_public(world):
    canon, pub, priv = world
    _sync(canon, [])
    for ref in ("refs/heads/main", "refs/heads/clean"):
        assert _has_ref(pub, ref) and _has_ref(priv, ref)


def test_state_sync_does_not_leak_the_prd_blob_to_public(world):
    canon, pub, priv = world
    _sync(canon, [])
    out = subprocess.run(
        ["git", "rev-list", "--all", "--objects"], cwd=pub, capture_output=True, text=True
    ).stdout
    assert "prd.json" not in out


def test_state_sync_unresolved_fork_point_refuses_on_public():
    calls = []

    class Runner:
        async def __call__(self, argv, cwd):
            calls.append(list(argv))
            if argv[1] == "for-each-ref":
                return 0, "refs/heads/x " + "a" * 40 + " \n"
            if argv[1] == "merge-base":
                return 128, ""
            return 0, ""

    lines = asyncio.run(
        MirrorHandler.sync_state(
            "https://forgejo.example/o/r.git", _dests(), runner=Runner(),
        )
    )
    pushes = [a for a in calls if a[1] == "push"]
    assert [p[2] for p in pushes] == ["https://gitlab.com/langevc/priv.git"]
    assert any(l.startswith("mirror-refused") for l in lines)
