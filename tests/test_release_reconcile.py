"""FFR-200-5: ReconcileHandler reconciles tags that exist but have no release.

Two parts: detection (read-only — it never creates, deletes, or otherwise
mutates a tag or a release) and reconciliation (creates the missing release
with the configured name, idempotently).
"""

from unittest.mock import AsyncMock

import pytest

from ops_engine.modules.reconcile import MirrorState, ReconcileHandler, ReconcileRecord


class _ReleaseConfig:
    """A placeholder-tolerant release config mirroring ReleaseConfig."""

    def __init__(self, *, name_template=None, draft=False):
        self.name_template = name_template
        self.draft = draft


@pytest.fixture
def adapter():
    adapter = AsyncMock()
    adapter.release_exists = AsyncMock(return_value=False)
    adapter.create_release = AsyncMock(return_value={"id": 1})
    return adapter


REPO = "langevc/ops-engine"


@pytest.mark.asyncio
async def test_detects_tag_without_release(adapter):
    adapter.release_exists.return_value = False
    missing = await ReconcileHandler.detect_missing_releases(adapter, REPO, ["v1.0.0"])
    assert missing == ["v1.0.0"]


@pytest.mark.asyncio
async def test_skips_tag_with_existing_release(adapter):
    adapter.release_exists.return_value = True
    missing = await ReconcileHandler.detect_missing_releases(adapter, REPO, ["v1.0.0"])
    assert missing == []


@pytest.mark.asyncio
async def test_partitions_tags_by_release_presence(adapter):
    async def release_exists(repo, tag_name):
        return tag_name == "v1.1.0"

    adapter.release_exists.side_effect = release_exists
    missing = await ReconcileHandler.detect_missing_releases(
        adapter, REPO, ["v1.0.0", "v1.1.0", "v1.2.0"]
    )
    assert missing == ["v1.0.0", "v1.2.0"]


@pytest.mark.asyncio
async def test_detection_is_read_only(adapter):
    adapter.release_exists.return_value = False
    await ReconcileHandler.detect_missing_releases(adapter, REPO, ["v1.0.0"])

    # The only adapter call the detection may make is the read-only existence
    # check; no mutating call may occur.
    assert [c[0] for c in adapter.method_calls] == ["release_exists"]
    adapter.create_release.assert_not_called()
    adapter.create_tag.assert_not_called()
    adapter.delete_release.assert_not_called()


@pytest.mark.asyncio
async def test_empty_tag_list_returns_empty(adapter):
    assert await ReconcileHandler.detect_missing_releases(adapter, REPO, []) == []


@pytest.mark.asyncio
async def test_missing_repo_returns_empty(adapter):
    adapter.release_exists.return_value = False
    assert await ReconcileHandler.detect_missing_releases(adapter, "", ["v1.0.0"]) == []
    adapter.release_exists.assert_not_called()


class TestReconcileCreatesMissingRelease:
    """Criterion 2: a reconcile run creates the missing release with the
    configured name, idempotently."""

    @pytest.mark.asyncio
    async def test_creates_release_with_configured_name(self, adapter):
        async def release_exists(repo, tag_name):
            return tag_name != "v1.0.0"

        adapter.release_exists.side_effect = release_exists
        config = _ReleaseConfig(name_template="fusionAIze Grid {tag_name}")

        created = await ReconcileHandler.reconcile_releases(
            adapter, REPO, ["v1.0.0", "v1.1.0"], config
        )

        assert created == ["v1.0.0"]
        adapter.create_release.assert_called_once()
        kwargs = adapter.create_release.call_args.kwargs
        assert kwargs["tag_name"] == "v1.0.0"
        assert kwargs["name"] == "fusionAIze Grid v1.0.0"

    @pytest.mark.asyncio
    async def test_default_name_when_no_template_configured(self, adapter):
        adapter.release_exists.return_value = False
        config = _ReleaseConfig()

        created = await ReconcileHandler.reconcile_releases(
            adapter, REPO, ["v1.0.0"], config
        )

        assert created == ["v1.0.0"]
        assert adapter.create_release.call_args.kwargs["name"] == "ops-engine v1.0.0"

    @pytest.mark.asyncio
    async def test_idempotent_no_create_when_release_exists(self, adapter):
        adapter.release_exists.return_value = True
        config = _ReleaseConfig(name_template="fusionAIze Grid {tag_name}")

        created = await ReconcileHandler.reconcile_releases(
            adapter, REPO, ["v1.0.0", "v1.1.0"], config
        )

        assert created == []
        adapter.create_release.assert_not_called()

    @pytest.mark.asyncio
    async def test_second_run_over_same_tags_creates_nothing(self, adapter):
        # First run sees the release missing and creates it.
        adapter.release_exists.return_value = False
        config = _ReleaseConfig(name_template="fusionAIze Grid {tag_name}")

        assert await ReconcileHandler.reconcile_releases(
            adapter, REPO, ["v1.0.0"], config
        ) == ["v1.0.0"]

        # Second run sees the release present and creates nothing new.
        adapter.release_exists.return_value = True
        adapter.create_release.reset_mock()
        assert await ReconcileHandler.reconcile_releases(
            adapter, REPO, ["v1.0.0"], config
        ) == []
        adapter.create_release.assert_not_called()


class TestStateIsQueryableRecord:
    """Criterion 3: release and mirror state are queryable as records, not only
    visible in a log."""

    @pytest.mark.asyncio
    async def test_query_state_returns_record_with_missing_releases(self, adapter):
        async def release_exists(repo, tag_name):
            return tag_name != "v1.0.0"

        adapter.release_exists.side_effect = release_exists
        adapter.get_latest_commit_sha = AsyncMock(return_value="abc123")

        record = await ReconcileHandler.query_state(
            adapter, REPO, ["v1.0.0", "v1.1.0"],
            mirror_url="github.com/LangeVC/ops-engine",
        )

        assert isinstance(record, ReconcileRecord)
        assert record.repo == REPO
        assert record.missing == ["v1.0.0"]
        assert record.created == []

    def test_record_serializes_to_dict(self):
        record = ReconcileRecord(
            repo=REPO,
            missing=["v1.0.0"],
            created=["v1.0.0"],
            mirror=MirrorState(
                repo=REPO, primary_sha="abc", mirror_sha="abc", in_sync=True
            ),
        )

        assert record.to_dict() == {
            "repo": REPO,
            "missing_releases": ["v1.0.0"],
            "created_releases": ["v1.0.0"],
            "mirror": {
                "repo": REPO,
                "primary_sha": "abc",
                "mirror_sha": "abc",
                "in_sync": True,
            },
        }

    @pytest.mark.asyncio
    async def test_query_mirror_state_in_sync(self, adapter):
        adapter.get_latest_commit_sha = AsyncMock(return_value="abc123")

        state = await ReconcileHandler.query_mirror_state(
            adapter, REPO, "github.com/LangeVC/ops-engine", primary_sha="abc123"
        )

        assert state.in_sync is True
        assert state.primary_sha == "abc123"
        assert state.mirror_sha == "abc123"

    @pytest.mark.asyncio
    async def test_query_mirror_state_detects_drift(self, adapter):
        async def latest(repo, branch="main"):
            return "abc123" if repo == REPO else "def456"

        adapter.get_latest_commit_sha = AsyncMock(side_effect=latest)

        state = await ReconcileHandler.query_mirror_state(
            adapter, REPO, "github.com/LangeVC/ops-engine"
        )

        assert state.in_sync is False
        assert state.primary_sha == "abc123"
        assert state.mirror_sha == "def456"

    @pytest.mark.asyncio
    async def test_query_mirror_state_no_url_is_not_synced(self, adapter):
        state = await ReconcileHandler.query_mirror_state(adapter, REPO, "")

        assert state.in_sync is False
        assert not adapter.get_latest_commit_sha.called

    @pytest.mark.asyncio
    async def test_query_mirror_state_unreachable_mirror_records_not_synced(self, adapter):
        async def latest(repo, branch="main"):
            if repo == REPO:
                return "abc123"
            raise RuntimeError("unreachable")

        adapter.get_latest_commit_sha = AsyncMock(side_effect=latest)

        state = await ReconcileHandler.query_mirror_state(
            adapter, REPO, "github.com/LangeVC/ops-engine"
        )

        assert state.in_sync is False
        assert state.primary_sha == "abc123"
        assert state.mirror_sha == ""
