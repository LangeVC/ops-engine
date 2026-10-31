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


DESTS = [Destination(forge="github", repo="LangeVC/ops-engine", role="mirror", visibility="private")]


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


# --- criterion 3: RED proof -----------------------------------------------------
#
# Real adapters (GitHub 100 rows/page as primary, Forgejo 50 rows/page as mirror)
# read from in-memory stores through a patched ``_request`` that honours the page
# parameters, so the fixture spans several pages on both sides.

N_TAGS = 230  # more than 50, three Forgejo pages, three GitHub pages


class PagedStore:
    """Branch and tag store served page by page in the shape of one forge."""

    def __init__(self, kind, branches, tags):
        self.kind, self.branches, self.tags = kind, dict(branches), dict(tags)

    def _rows(self, path):
        if path.endswith("/branches"):
            if self.kind == "github":
                return [{"name": n, "commit": {"sha": s}} for n, s in sorted(self.branches.items())]
            return [{"name": n, "commit": {"id": s}} for n, s in sorted(self.branches.items())]
        return [{"name": n, "id": "tagobj", "commit": {"sha": s}} for n, s in sorted(self.tags.items())]

    async def request(self, method, path, **kw):
        params = kw["params"]
        size = int(params["per_page"] if self.kind == "github" else params["limit"])
        page = int(params["page"])
        return _resp(self._rows(path)[(page - 1) * size : page * size])


def _tags(n=N_TAGS):
    return {f"v{i:04d}": f"{i:040x}" for i in range(n)}


def _stores(mirror_tags, mirror_main="a" * 40):
    primary = PagedStore("github", {"main": "a" * 40}, _tags())
    mirror = PagedStore("forgejo", {"main": mirror_main}, mirror_tags)
    return primary, mirror


def _adapters(primary, mirror):
    gh = GithubAdapter(token="t", webhook_secret="s")
    fj = ForgejoAdapter(base_url="https://git.example.com", token="t", webhook_secret="s")
    return (
        gh, patch.object(gh, "_request", new=AsyncMock(side_effect=primary.request)),
        fj, patch.object(fj, "_request", new=AsyncMock(side_effect=mirror.request)),
    )


async def _single_page_drift(primary_adapter, mirror_adapter):
    """Test-local single-page measurement: only page 1 of each list is read."""

    async def first_page(adapter, path, params):
        return {r["name"] for r in (await adapter._request("GET", path, params=params)).json()}

    want = await first_page(primary_adapter, "/repos/o/r/tags", {"per_page": "100", "page": "1"})
    have = await first_page(mirror_adapter, "/repos/m/r/tags", {"limit": "50", "page": "1"})
    return sorted(want - have)


@pytest.mark.asyncio
async def test_single_page_measurement_reports_false_drift_real_measurement_none():
    primary, mirror = _stores(_tags())  # mirror holds every tag: at parity
    gh, p1, fj, p2 = _adapters(primary, mirror)
    with p1, p2:
        false_drift = await _single_page_drift(gh, fj)
        assert len(false_drift) == 50  # page 1 is 100 rows on GitHub, 50 on Forgejo
        rep = await ReconcileHandler.measure_drift(gh, "o/r", fj, "m/r")
    assert not rep.behind and not rep.missing and not rep.differing and not rep.extra


@pytest.mark.asyncio
async def test_stopped_layover_stale_mirror_is_at_parity_one_cycle_after_restart():
    # While the layover was stopped the primary gained 130 tags and main moved.
    stale = {k: v for k, v in list(_tags().items())[:100]}
    primary, mirror = _stores(stale, mirror_main="0" * 40)

    pf, mf = FakeForge(primary.branches, primary.tags), FakeForge(mirror.branches, mirror.tags)
    runner = PushingRunner(pf, mf)
    gh, p1, fj, p2 = _adapters(primary, mirror)

    async def runner_on_stores(argv, cwd):
        out = await runner(argv, cwd)
        if argv[1] == "push":
            mirror.branches.update(primary.branches)
            mirror.tags.update(primary.tags)
        return out

    with p1, p2:
        before = await ReconcileHandler.measure_drift(gh, "o/r", fj, "m/r")
        assert before.behind and len(before.missing) == N_TAGS - 100
        assert before.differing == ["refs/heads/main"]
        rep = await ReconcileHandler.reconcile_mirror(
            gh, "o/r", fj, "m/r", CANON, DESTS, runner=runner_on_stores
        )
    assert len([c for c in runner.calls if c[1] == "push"]) == 1  # one cycle, one sync
    assert not rep.behind and not rep.missing and not rep.differing
