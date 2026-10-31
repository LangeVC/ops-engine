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


def test_state_sync_unreadable_history_refuses_on_public():
    calls = []

    class Runner:
        async def __call__(self, argv, cwd):
            calls.append(list(argv))
            if argv[1] == "for-each-ref":
                return 0, "refs/heads/x " + "a" * 40 + " \n"
            if argv[1] == "log":
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


# --- OE-035 criteria 2 and 3 -----------------------------------------------------
#
# Criterion 2: reconcile_mirror measures and syncs ONE destination list. Criterion
# 3: a destination whose visibility is unset or not public/private is refused at
# configuration load and by the gate.

from ops_engine.config_loader import (  # noqa: E402
    ConfigSectionError,
    OpsEngineConfig,
    RepoConfig,
)
from ops_engine.modules.reconcile import ReconcileHandler  # noqa: E402

CANON_URL = "https://forgejo.example/o/r.git"


class _Forge:
    def __init__(self, branches, tags=None):
        self.branches = dict(branches)
        self.tags = dict(tags or {})

    async def list_branches(self, repo):
        return dict(self.branches)

    async def list_tags(self, repo):
        return dict(self.tags)


class _Runner:
    def __init__(self):
        self.calls = []

    async def __call__(self, argv, cwd):
        self.calls.append(list(argv))
        return 0, ""


def _pub():
    return Destination(forge="github", repo="LangeVC/pub", role="mirror", visibility="public")


def _other():
    return Destination(forge="gitlab", repo="langevc/other", role="mirror", visibility="private")


def test_reconcile_mirror_refuses_different_lists_for_measuring_and_syncing():
    from ops_engine.modules.reconcile import MirrorListMismatchError

    primary = _Forge({"main": "a" * 40})
    mirror = _Forge({"main": "0" * 40})
    runner = _Runner()
    with pytest.raises(MirrorListMismatchError):
        asyncio.run(
            ReconcileHandler.reconcile_mirror(
                primary, "o/r", mirror, "LangeVC/pub", CANON_URL, [_pub()],
                runner=runner, measure_destinations=[_other()],
            )
        )
    assert runner.calls == []  # refused before any git call


def test_reconcile_mirror_accepts_the_same_list_for_both():
    primary = _Forge({"main": "a" * 40})
    mirror = _Forge({"main": "a" * 40})
    dests = [_pub()]
    report = asyncio.run(
        ReconcileHandler.reconcile_mirror(
            primary, "o/r", mirror, "LangeVC/pub", CANON_URL, dests,
            runner=_Runner(), measure_destinations=list(dests),
        )
    )
    assert not report.behind


@pytest.mark.parametrize("visibility", ["", "internal", "PUBLIC"])
def test_push_event_refuses_and_audits_a_destination_with_undeclared_visibility(visibility):
    dest = Destination(forge="github", repo="LangeVC/x", role="mirror", visibility=visibility)
    runner = _Runner()
    audit = []
    event = {
        "event_type": "push",
        "raw": {"ref": "refs/heads/main", "after": "a" * 40, "before": "b" * 40},
    }
    lines = asyncio.run(
        MirrorHandler.push_event(event, [dest], repo_dir="/x", runner=runner, audit=audit)
    )
    assert [a for a in runner.calls if a[1] == "push"] == []
    assert len(lines) == 1 and lines == audit
    assert lines[0].startswith("mirror-refused") and "reason=visibility-undeclared" in lines[0]


def test_sync_state_refuses_and_audits_a_destination_with_empty_visibility():
    dest = Destination(forge="github", repo="LangeVC/x", role="mirror", visibility="")
    runner = _Runner()
    audit = []
    asyncio.run(
        MirrorHandler.sync_state(CANON_URL, [dest], runner=runner, audit=audit)
    )
    assert [a for a in runner.calls if a[1] == "push"] == []
    assert any("reason=visibility-undeclared" in l for l in audit)


def test_sweep_refuses_a_destination_with_empty_visibility():
    class SweepRunner(_Runner):
        async def __call__(self, argv, cwd):
            self.calls.append(list(argv))
            if argv[1] == "for-each-ref":
                return 0, "refs/heads/main\n"
            return 0, ""

    dest = Destination(forge="github", repo="LangeVC/x", role="mirror", visibility="")
    runner = SweepRunner()
    audit = []
    asyncio.run(MirrorHandler.sweep_deleted_refs(CANON_URL, [dest], runner=runner, audit=audit))
    assert not any(a[1] in ("ls-remote", "push") for a in runner.calls)
    assert any("reason=visibility-undeclared" in l for l in audit)


def test_unset_or_unknown_visibility_counts_as_public():
    for value in ("", "internal"):
        dest = Destination(forge="github", repo="x/y", visibility=value)
        assert MirrorHandler.effective_visibility(dest) == "public"
    assert MirrorHandler.effective_visibility(_other()) == "private"


@pytest.mark.parametrize("visibility", ["", "internal"])
def test_config_load_refuses_a_destination_with_undeclared_visibility(visibility):
    raw = {
        "orgs": {
            "o": {
                "repositories": {
                    "r": {
                        "destinations": [
                            {"forge": "github", "repo": "o/r", "role": "mirror",
                             "visibility": visibility}
                        ]
                    }
                }
            }
        }
    }
    with pytest.raises(ConfigSectionError):
        OpsEngineConfig.load(raw)
    with pytest.raises(ValueError):
        RepoConfig(destinations=[Destination(repo="o/r", visibility=visibility)])


def test_reconcile_mirror_refuses_a_measured_mirror_that_is_not_a_synced_destination():
    from ops_engine.modules.reconcile import MirrorListMismatchError

    primary = _Forge({"main": "a" * 40})
    mirror = _Forge({"main": "0" * 40})
    runner = _Runner()
    synced = Destination(
        forge="github", repo="synced/TARGET", role="mirror", visibility="private"
    )
    with pytest.raises(MirrorListMismatchError):
        asyncio.run(
            ReconcileHandler.reconcile_mirror(
                primary, "o/r", mirror, "measured/ELSEWHERE", CANON_URL, [synced],
                runner=runner,
            )
        )
    assert runner.calls == []


def test_reconcile_mirror_refuses_a_measured_repo_that_is_only_a_non_mirror_destination():
    from ops_engine.modules.reconcile import MirrorListMismatchError

    release = Destination(
        forge="github", repo="o/rel", role="release", visibility="private"
    )
    with pytest.raises(MirrorListMismatchError):
        asyncio.run(
            ReconcileHandler.reconcile_mirror(
                _Forge({"main": "a" * 40}), "o/r", _Forge({"main": "0" * 40}), "o/rel",
                CANON_URL, [release], runner=_Runner(),
            )
        )


@pytest.mark.parametrize("alias", [{"github": "o/legacy"}, {"mirror_url": "o/legacy"}])
def test_config_load_refuses_the_deprecated_mirror_alias_without_visibility(alias):
    from ops_engine.config_loader import MirrorConfig

    with pytest.raises(ValueError):
        RepoConfig(mirror=MirrorConfig(**alias))
    with pytest.raises(ConfigSectionError):
        OpsEngineConfig.load({"orgs": {"o": {"repositories": {"r": {"mirror": alias}}}}})
    ok = RepoConfig(mirror=MirrorConfig(visibility="private", **alias))
    assert ok.mirror.visibility == "private"


# --- reconcile_mirror passes the repository's gate configuration on --------------

def _gated_world(tmp_path, monkeypatch, default):
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", default)
    (work / "a.txt").write_text("a\n")
    _git(work, "add", ".")
    _git(work, *ident, "commit", "-q", "-m", "base")
    _git(work, "checkout", "-q", "-b", "feat")
    (work / "secret").mkdir()
    (work / "secret" / "key.txt").write_text("k\n")
    _git(work, "add", ".")
    _git(work, *ident, "commit", "-q", "-m", "feat")
    canon, pub, priv = tmp_path / "canon.git", tmp_path / "pub.git", tmp_path / "priv.git"
    for bare in (canon, pub, priv):
        _git(tmp_path, "init", "-q", "--bare", str(bare))
    _git(work, "push", "-q", str(canon), default, "feat")
    rewrites = {
        "https://github.com/LangeVC/pub.git": f"file://{pub}",
        "https://gitlab.com/langevc/priv.git": f"file://{priv}",
    }
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(rewrites)))
    for i, (url, local) in enumerate(rewrites.items()):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{i}", f"url.{local}.insteadOf")
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{i}", url)
    return canon, pub, priv


def _reconcile(canon, **kw):
    return asyncio.run(
        ReconcileHandler.reconcile_mirror(
            _Forge({"main": "a" * 40}), "o/r", _Forge({"main": "0" * 40}),
            "LangeVC/pub", f"file://{canon}", _dests(), **kw
        )
    )


def test_reconcile_mirror_applies_the_configured_candidate_globs(tmp_path, monkeypatch):
    canon, pub, priv = _gated_world(tmp_path, monkeypatch, "main")
    audit = []
    _reconcile(canon, audit=audit, candidate_globs=["secret/*"])
    assert not _has_ref(pub, "refs/heads/feat")  # RED: the default globs let it through
    assert _has_ref(pub, "refs/heads/main")
    assert _has_ref(priv, "refs/heads/feat")
    assert any(l.startswith("mirror-refused") and "secret/key.txt" in l for l in audit)


def test_reconcile_mirror_honours_a_non_main_default_branch(tmp_path, monkeypatch):
    canon, pub, priv = _gated_world(tmp_path, monkeypatch, "trunk")
    audit = []
    _reconcile(canon, audit=audit, default_branch="trunk", candidate_globs=["nothing/*"])
    # RED: with default "main" every ref is refused as 'fork point unresolved'.
    assert _has_ref(pub, "refs/heads/trunk") and _has_ref(pub, "refs/heads/feat")
    assert not any(l.startswith("mirror-refused") for l in audit)


# --- a branch forked from a substrate-carrying default branch ---------------------

def _substrate_main_world(tmp_path, monkeypatch, clean_main):
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "a.txt").write_text("a\n")
    if not clean_main:
        (work / ".skillweave").mkdir()
        (work / ".skillweave" / "prd.json").write_text('{"secret": 1}\n')
    _git(work, "add", ".")
    _git(work, *ident, "commit", "-q", "-m", "main")
    _git(work, "checkout", "-q", "-b", "feat")
    (work / "b.txt").write_text("b\n")
    _git(work, "add", ".")
    _git(work, *ident, "commit", "-q", "-m", "feat")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, capture_output=True,
                         text=True, check=True).stdout.strip()
    canon, pub, priv = tmp_path / "canon.git", tmp_path / "pub.git", tmp_path / "priv.git"
    for bare in (canon, pub, priv):
        _git(tmp_path, "init", "-q", "--bare", str(bare))
    _git(work, "push", "-q", str(canon), "main", "feat")
    rewrites = {
        "https://github.com/LangeVC/pub.git": f"file://{pub}",
        "https://gitlab.com/langevc/priv.git": f"file://{priv}",
    }
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(rewrites)))
    for i, (url, local) in enumerate(rewrites.items()):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{i}", f"url.{local}.insteadOf")
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{i}", url)
    return work, canon, pub, priv, sha


def _objects(bare):
    return subprocess.run(["git", "rev-list", "--all", "--objects"], cwd=bare,
                          capture_output=True, text=True, check=True).stdout


def test_state_sync_refuses_a_branch_forked_from_a_substrate_main(tmp_path, monkeypatch):
    work, canon, pub, priv, sha = _substrate_main_world(tmp_path, monkeypatch, False)
    audit = []
    _sync(canon, audit)
    assert not _has_ref(pub, "refs/heads/feat") and not _has_ref(pub, "refs/heads/main")
    assert "prd.json" not in _objects(pub)
    assert _has_ref(priv, "refs/heads/feat")
    assert any("ref=refs/heads/feat" in l and l.startswith("mirror-refused") for l in audit)


def test_push_event_refuses_a_branch_forked_from_a_substrate_main(tmp_path, monkeypatch):
    work, canon, pub, priv, sha = _substrate_main_world(tmp_path, monkeypatch, False)
    event = {"event_type": "push", "raw": {"ref": "refs/heads/feat", "after": sha,
                                           "before": "0" * 40}}
    audit = []
    asyncio.run(MirrorHandler.push_event(event, _dests(), repo_dir=str(work), audit=audit))
    assert "prd.json" not in _objects(pub)
    assert not _has_ref(pub, "refs/heads/feat")
    assert _has_ref(priv, "refs/heads/feat")
    assert any(l.startswith("mirror-refused") for l in audit)


def test_clean_branch_from_clean_main_still_reaches_public(tmp_path, monkeypatch):
    work, canon, pub, priv, sha = _substrate_main_world(tmp_path, monkeypatch, True)
    _sync(canon, [])
    assert _has_ref(pub, "refs/heads/feat") and _has_ref(pub, "refs/heads/main")
    event = {"event_type": "push", "raw": {"ref": "refs/heads/feat", "after": sha,
                                           "before": "0" * 40}}
    lines = asyncio.run(MirrorHandler.push_event(event, _dests(), repo_dir=str(work)))
    assert not any(l.startswith("mirror-refused") for l in lines)
