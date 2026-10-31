"""OE-024 criteria 1 and 2: drift measured across every page, stale mirror synced.

Adapter tests feed multi-page responses through a patched ``_request`` (no
network). Reconcile tests use in-memory fake adapters; the fake git runner
applies a push to the fake mirror so the post-sync measurement is real.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
from ops_engine.adapters.github_adapter import GithubAdapter
from ops_engine.config_loader import Destination
from ops_engine.modules.reconcile import ReconcileHandler

CANON = "https://forgejo.example/langevc/ops-engine.git"


def _resp(rows):
    r = MagicMock()
    r.json.return_value = rows
    r.raise_for_status = MagicMock()
    return r


def _paged(pages):
    """Return a ``_request`` side effect serving ``pages`` by ?page=N, then empty."""

    async def fake(method, path, **kw):
        n = int(kw["params"]["page"])
        return _resp(pages[n - 1] if n <= len(pages) else [])

    return fake


# --- criterion 1: adapters walk every page -----------------------------------


@pytest.mark.asyncio
async def test_github_branches_and_tags_walk_all_pages():
    gh = GithubAdapter(token="t", webhook_secret="s")
    branches = [
        [{"name": "a", "commit": {"sha": "1" * 40}}, {"name": "b", "commit": {"sha": "2" * 40}}],
        [{"name": "c", "commit": {"sha": "3" * 40}}],
    ]
    tags = [
        [{"name": "v1", "commit": {"sha": "4" * 40}}],
        [{"name": "v2", "commit": {"sha": "5" * 40}}],
        [{"name": "v3", "commit": {"sha": "6" * 40}}],
    ]
    with patch.object(gh, "_request", new=AsyncMock(side_effect=_paged(branches))):
        assert await gh.list_branches("o/r") == {"a": "1" * 40, "b": "2" * 40, "c": "3" * 40}
    with patch.object(gh, "_request", new=AsyncMock(side_effect=_paged(tags))):
        assert set(await gh.list_tags("o/r")) == {"v1", "v2", "v3"}


@pytest.mark.asyncio
async def test_forgejo_walks_all_pages_even_when_server_caps_page_size():
    fj = ForgejoAdapter(base_url="https://git.example.com", token="t", webhook_secret="s")
    # The server returns 1 row per page although more were asked for: a short
    # page must not be read as the last page.
    branches = [[{"name": f"b{i}", "commit": {"id": f"{i}" * 40}}] for i in range(1, 5)]
    tags = [[{"name": f"v{i}", "id": "tagobj", "commit": {"sha": f"{i}" * 40}}] for i in range(1, 4)]
    with patch.object(fj, "_request", new=AsyncMock(side_effect=_paged(branches))):
        assert len(await fj.list_branches("o/r")) == 4
    with patch.object(fj, "_request", new=AsyncMock(side_effect=_paged(tags))):
        got = await fj.list_tags("o/r")
    # dereferenced: the commit SHA, never the tag object id
    assert got == {"v1": "1" * 40, "v2": "2" * 40, "v3": "3" * 40}


# --- criterion 1: drift measurement -------------------------------------------


class FakeForge:
    def __init__(self, branches, tags):
        self.branches = dict(branches)
        self.tags = dict(tags)

    async def list_branches(self, repo):
        return dict(self.branches)

    async def list_tags(self, repo):
        return dict(self.tags)


@pytest.mark.asyncio
async def test_measure_drift_compares_names_and_shas_of_heads_and_tags():
    primary = FakeForge({"main": "a" * 40, "feat": "b" * 40}, {"v1": "c" * 40, "v2": "d" * 40})
    mirror = FakeForge({"main": "f" * 40, "old": "e" * 40}, {"v1": "c" * 40})
    rep = await ReconcileHandler.measure_drift(primary, "o/r", mirror, "m/r")
    assert rep.missing == ["refs/heads/feat", "refs/tags/v2"]
    assert rep.differing == ["refs/heads/main"]
    assert rep.extra == ["refs/heads/old"]
    assert rep.behind is True


@pytest.mark.asyncio
async def test_identical_forges_have_no_drift_and_extra_only_is_not_behind():
    same = {"main": "a" * 40}
    rep = await ReconcileHandler.measure_drift(
        FakeForge(same, {"v1": "c" * 40}), "o/r", FakeForge(same, {"v1": "c" * 40}), "m/r"
    )
    assert not rep.behind and not rep.missing and not rep.differing
    rep = await ReconcileHandler.measure_drift(
        FakeForge(same, {}), "o/r", FakeForge({**same, "x": "9" * 40}, {}), "m/r"
    )
    assert rep.extra == ["refs/heads/x"] and rep.behind is False


@pytest.mark.asyncio
async def test_tag_with_same_name_but_other_sha_is_drift():
    rep = await ReconcileHandler.measure_drift(
        FakeForge({}, {"v1": "a" * 40}), "o/r", FakeForge({}, {"v1": "b" * 40}), "m/r"
    )
    assert rep.differing == ["refs/tags/v1"]


# --- criterion 2: behind mirror is state-synced in one cycle ------------------


class PushingRunner:
    """Fake git runner; a push to the mirror copies the primary refs onto it."""

    def __init__(self, primary, mirror):
        self.primary, self.mirror = primary, mirror
        self.calls = []

    async def __call__(self, argv, cwd):
        self.calls.append(list(argv))
        if argv[1] == "push":
            self.mirror.branches.update(self.primary.branches)
            self.mirror.tags.update(self.primary.tags)
        return 0, ""


DESTS = [Destination(forge="github", repo="LangeVC/ops-engine", role="mirror")]


@pytest.mark.asyncio
async def test_stale_mirror_is_synced_within_one_cycle():
    primary = FakeForge({"main": "a" * 40}, {"v1": "c" * 40})
    mirror = FakeForge({"main": "0" * 40}, {})
    runner = PushingRunner(primary, mirror)
    audit: list[str] = []
    rep = await ReconcileHandler.reconcile_mirror(
        primary, "o/r", mirror, "m/r", CANON, DESTS, runner=runner, audit=audit
    )
    pushes = [c for c in runner.calls if c[1] == "push"]
    assert len(pushes) == 1  # exactly one sync in the cycle
    assert "+refs/tags/*:refs/tags/*" in pushes[0]
    assert rep.behind is False and mirror.branches == primary.branches
    assert mirror.tags == primary.tags
    assert any(line.startswith("mirror-state-push") for line in audit)


@pytest.mark.asyncio
async def test_in_sync_mirror_triggers_no_git_call():
    refs = ({"main": "a" * 40}, {"v1": "c" * 40})
    primary, mirror = FakeForge(*refs), FakeForge(*refs)
    runner = PushingRunner(primary, mirror)
    rep = await ReconcileHandler.reconcile_mirror(
        primary, "o/r", mirror, "m/r", CANON, DESTS, runner=runner
    )
    assert runner.calls == [] and rep.behind is False
