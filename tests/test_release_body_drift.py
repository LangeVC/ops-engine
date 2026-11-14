"""P36-018 criteria 1-3: Run summary body state, drift check, create-race regression.

Criterion 1: The per-destination run summary lists the body state per destination:
written, supplemented, already equal, or failed with the named difference.

Criterion 2: The drift check reports a release object on any destination whose
body is empty or lacks the CHANGELOG section, without writing.

Criterion 3: A regression test with no network covers the create race: the
release already exists (409 on Forgejo, 422 on GitHub) with an empty body.

RED PROOF (criterion 4, later dispatch): the 3.5.2 summary and drift check do
not mention the body; the new tests fail there.
"""

import json

import httpx
import pytest

from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
from ops_engine.adapters.github_adapter import GithubAdapter
from ops_engine.config_loader import OpsEngineConfig
from ops_engine.modules.release import PublicationReport, ReleaseHandler

REPO = "langevc/ops-engine"
TAG = "v3.5.1"
TITLE = "ops-engine v3.5.1"
NOTES = "- A feature.\n\n- A fix.\n"
INSTALL_BLOCK = "pip install ops-engine"
ASSETS_LINE = "[ops_engine-3.5.1-py3-none-any.whl](https://example.com/asset)"
CHANGELOG_PATH = "CHANGELOG.md"
OTHER_BODY = "Some other release body without the changelog section."
RELEASE_ID = 77
FILES = ["ops_engine-3.5.1-py3-none-any.whl", "SHA256SUMS"]


def _full_body():
    return (
        f"## {TITLE}\n\n"
        f"{NOTES}\n"
        f"### Install\n"
        f"{INSTALL_BLOCK}\n\n"
        f"{ASSETS_LINE}\n\n"
        f"See [{CHANGELOG_PATH}](https://example.com/{CHANGELOG_PATH}) for all changes.\n"
    )


FULL_BODY = _full_body()


def _config(*, forge="forgejo"):
    return OpsEngineConfig.load(
        {
            "orgs": {
                "langevc": {
                    "repositories": {
                        "ops-engine": {
                            "destinations": [
                                {
                                    "forge": forge,
                                    "repo": REPO,
                                    "role": "release",
                                    "visibility": "public",
                                    "credential_variable": "OPS_TOKEN",
                                }
                            ]
                        }
                    }
                }
            }
        }
    )


def _release(*, body, **over):
    release = {
        "id": RELEASE_ID,
        "tag_name": TAG,
        "name": TITLE,
        "body": body,
        "draft": False,
        "prerelease": False,
        "assets": [],
        "tarball_url": f"https://forge.invalid/{REPO}/archive/{TAG}.tar.gz",
        "zipball_url": f"https://forge.invalid/{REPO}/archive/{TAG}.zip",
    }
    release.update(over)
    return release


class RaceFakeDestination:
    """Simulates a create race: the release doesn't exist at check time (first
    GET returns 404), the create returns 409/422 (race — the release was
    created between check and create), and the subsequent lookup finds the
    release with an empty body."""

    def __init__(self, *, body="", create_status=409):
        self._body = body
        self._create_status = create_status
        self._get_count = 0
        self.created_body = None
        self.patched_bodies = []
        self.calls = []
        self.uploads = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if request.method == "GET" and "/releases/tags/" in path:
            self._get_count += 1
            if self._get_count == 1:
                # First lookup: release does not exist yet.
                return httpx.Response(404, json={"message": "not found"})
            # Subsequent lookup: release was created (race).
            return httpx.Response(
                200,
                json={
                    "id": RELEASE_ID,
                    "tag_name": TAG,
                    "name": TITLE,
                    "body": self._body,
                },
            )
        if request.method == "GET" and path.endswith("/releases"):
            return httpx.Response(200, json=[])
        if request.method == "PATCH" and f"/releases/{RELEASE_ID}" in path:
            body = json.loads(request.content)
            if "body" in body:
                self.patched_bodies.append(body["body"])
            return httpx.Response(200, json={"id": RELEASE_ID})
        if request.method == "POST" and f"/releases/{RELEASE_ID}/assets" in path:
            name = request.url.params.get("name")
            self.uploads.append(name)
            return httpx.Response(201, json={"name": name})
        if request.method == "POST" and path.endswith("/releases"):
            if self._create_status:
                return httpx.Response(
                    self._create_status, json={"message": "conflict"}
                )
            body = json.loads(request.content)
            self.created_body = body.get("body")
            return httpx.Response(
                201, json={"id": RELEASE_ID, "tag_name": TAG, "name": TITLE}
            )
        return httpx.Response(500, json={"message": f"unexpected {request.method} {path}"})

    def writes(self):
        return [(m, p) for m, p in self.calls if m != "GET"]

    def creates(self):
        return sum(1 for m, p in self.calls if m == "POST" and p.endswith("/releases"))


def _forgejo(dest):
    adapter = ForgejoAdapter(base_url="https://forge.invalid", token="t", webhook_secret="s")
    adapter._client = httpx.AsyncClient(
        base_url="https://forge.invalid/api/v1", transport=httpx.MockTransport(dest.handler)
    )
    return adapter


def _github(dest):
    adapter = GithubAdapter(token="t", webhook_secret="s")
    adapter._client = httpx.AsyncClient(
        base_url="https://api.github.invalid", transport=httpx.MockTransport(dest.handler)
    )
    return adapter


async def _publish(forge, adapter, tmp_path, *, release_body=None, tag_name=None, **over):
    art = tmp_path / "dist"
    art.mkdir(exist_ok=True)
    for name in FILES:
        (art / name).write_bytes(name.encode())
    audit = []
    kwargs = {"release_body": release_body, **over} if release_body else over
    report = await ReleaseHandler.publish_release(
        _config(forge=forge), REPO, tag_name or TAG, NOTES, art,
        adapters=[adapter], skip_existing=True, audit=audit, **kwargs,
    )
    return report, audit


# ---------------------------------------------------------------------------
# Criterion 3: create-race regression test (409 Forgejo, 422 GitHub)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_race_empty_body_supplemented_forgejo(tmp_path):
    """Criterion 3: the release does not exist at check time (404), the create
    returns 409 (race — a distribution workflow created it between check and
    create), and the existing release has an empty body. The body is
    supplemented, not failed, and no duplicate release is created."""
    dest = RaceFakeDestination(body="", create_status=409)
    report, _ = await _publish("forgejo", _forgejo(dest), tmp_path,
                               release_body=FULL_BODY)

    label = f"forgejo:{REPO}"
    # The body was supplemented via PATCH.
    assert len(dest.patched_bodies) >= 1, "empty body was not supplemented"
    patched = dest.patched_bodies[0]
    assert f"## {TITLE}" in patched
    assert NOTES.strip() in patched
    # The create attempt got 409 (race), then the body was judged and
    # supplemented — no duplicate release was successfully created.
    assert dest.creates() == 1  # the initial attempt that got 409
    assert report.supplemented == [label]
    assert report.failed == []
    # RED PROOF (criterion 4): body_states attribute is new in 3.6.0.
    assert report.body_states.get(label) == "supplemented"


@pytest.mark.asyncio
async def test_create_race_empty_body_supplemented_github(tmp_path):
    """Criterion 3: same race on GitHub (422 instead of 409). The body is
    supplemented and the destination is not failed."""
    dest = RaceFakeDestination(body="", create_status=422)
    report, _ = await _publish("github", _github(dest), tmp_path,
                               release_body=FULL_BODY)

    label = f"github:{REPO}"
    assert len(dest.patched_bodies) >= 1, "empty body was not supplemented"
    patched = dest.patched_bodies[0]
    assert f"## {TITLE}" in patched
    assert NOTES.strip() in patched
    assert dest.creates() == 1  # the initial attempt that got 422
    assert report.supplemented == [label]
    assert report.failed == []
    # RED PROOF (criterion 4): body_states attribute is new in 3.6.0.
    assert report.body_states.get(label) == "supplemented"


@pytest.mark.asyncio
async def test_create_race_non_empty_body_fails_for_missing_changelog_forgejo(tmp_path):
    """Criterion 3: race with a body that lacks the CHANGELOG section. The
    destination is failed, not supplemented, and no writes occur."""
    dest = RaceFakeDestination(body=OTHER_BODY, create_status=409)
    report, _ = await _publish("forgejo", _forgejo(dest), tmp_path,
                               release_body=FULL_BODY)

    label = f"forgejo:{REPO}"
    # No body supplement writes — the release is left untouched.
    assert len(dest.patched_bodies) == 0
    assert dest.creates() == 1  # the initial attempt that got 409
    assert label in report.failed
    detail = report.failure_details.get(label, "")
    assert "CHANGELOG" in detail or "changelog" in detail.lower()
    # RED PROOF (criterion 4): body_states attribute is new in 3.6.0.
    state = report.body_states.get(label, "")
    assert state.startswith("failed:")


# ---------------------------------------------------------------------------
# Criterion 1: per-destination run summary lists the body state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_body_state_written_on_create_success(tmp_path):
    """Criterion 1: a newly created release records 'written'."""
    dest = RaceFakeDestination(body="", create_status=0)
    report, _ = await _publish("forgejo", _forgejo(dest), tmp_path,
                               release_body=FULL_BODY)

    label = f"forgejo:{REPO}"
    assert report.published == [label]
    assert report.body_states.get(label) == "written"


@pytest.mark.asyncio
async def test_body_state_supplemented_when_empty_body_filled(tmp_path):
    """Criterion 1: an empty body that gets supplemented records 'supplemented'."""
    dest = RaceFakeDestination(body="", create_status=409)
    report, _ = await _publish("forgejo", _forgejo(dest), tmp_path,
                               release_body=FULL_BODY)

    label = f"forgejo:{REPO}"
    assert report.supplemented == [label]
    assert report.body_states.get(label) == "supplemented"


@pytest.mark.asyncio
async def test_body_state_failed_when_changelog_missing(tmp_path):
    """Criterion 1: a body without the CHANGELOG section records 'failed' with
    the named difference."""
    dest = RaceFakeDestination(body=OTHER_BODY, create_status=409)
    report, _ = await _publish("forgejo", _forgejo(dest), tmp_path,
                               release_body=FULL_BODY)

    label = f"forgejo:{REPO}"
    state = report.body_states.get(label, "")
    assert state.startswith("failed:")
    assert "CHANGELOG" in state or "changelog" in state.lower()


class ExistingReleaseDestination:
    """A destination where the release already exists with a given body and no
    race occurs (first GET returns 200)."""

    def __init__(self, *, body):
        self._body = body
        self.patched_bodies = []
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if request.method == "GET" and "/releases/tags/" in path:
            return httpx.Response(
                200,
                json={
                    "id": RELEASE_ID,
                    "tag_name": TAG,
                    "name": TITLE,
                    "body": self._body,
                },
            )
        if request.method == "GET" and path.endswith("/releases"):
            return httpx.Response(200, json=[{
                "id": RELEASE_ID, "tag_name": TAG, "name": TITLE,
                "body": self._body,
            }])
        if request.method == "PATCH" and f"/releases/{RELEASE_ID}" in path:
            body = json.loads(request.content)
            if "body" in body:
                self.patched_bodies.append(body["body"])
            return httpx.Response(200, json={"id": RELEASE_ID})
        if request.method == "POST" and f"/releases/{RELEASE_ID}/assets" in path:
            return httpx.Response(201, json={"name": "asset"})
        if request.method == "POST" and path.endswith("/releases"):
            return httpx.Response(409, json={"message": "conflict"})
        return httpx.Response(500, json={"message": f"unexpected {request.method} {path}"})

    def writes(self):
        return [(m, p) for m, p in self.calls if m != "GET"]


@pytest.mark.asyncio
async def test_body_state_already_equal_when_changelog_present(tmp_path):
    """Criterion 1: a release whose body already contains the CHANGELOG section
    records 'already_equal'."""
    dest = ExistingReleaseDestination(body=FULL_BODY)
    report, _ = await _publish("forgejo", _forgejo(dest), tmp_path,
                               release_body=FULL_BODY)

    label = f"forgejo:{REPO}"
    assert report.body_states.get(label) == "already_equal"
    # No body PATCH was issued — the release was left untouched.
    assert len(dest.patched_bodies) == 0


# ---------------------------------------------------------------------------
# Criterion 2: read-only drift check for release bodies
# ---------------------------------------------------------------------------


class DriftCheckFakeDestination:
    """Read-only destination for drift check tests — only serves GET."""

    def __init__(self, *, body=None, has_release=True):
        self._body = body
        self._has_release = has_release
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if request.method == "GET" and "/releases/tags/" in path:
            if not self._has_release:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(
                200,
                json={
                    "id": RELEASE_ID,
                    "tag_name": TAG,
                    "name": TITLE,
                    "body": self._body,
                },
            )
        if request.method == "GET" and path.endswith("/releases"):
            if not self._has_release:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[{
                "id": RELEASE_ID, "tag_name": TAG, "name": TITLE,
                "body": self._body,
            }])
        return httpx.Response(500, json={"message": f"unexpected {request.method} {path}"})

    def writes(self):
        return [(m, p) for m, p in self.calls if m != "GET"]


from ops_engine.modules.reconcile import ReconcileHandler


@pytest.mark.asyncio
async def test_drift_check_reports_empty_body():
    """Criterion 2: an empty body is reported with empty=True."""
    dest = DriftCheckFakeDestination(body="")
    adapter = _forgejo(dest)

    result = await ReconcileHandler.check_release_body(
        adapter, REPO, TAG, release_notes=NOTES,
    )

    assert result is not None
    assert result.empty is True
    assert result.missing_changelog is False
    assert result.repo == REPO
    assert result.tag_name == TAG
    assert result.release_id == RELEASE_ID
    # Read-only: no writes occurred.
    assert dest.writes() == []


@pytest.mark.asyncio
async def test_drift_check_reports_missing_changelog():
    """Criterion 2: a body without the CHANGELOG section is reported with
    missing_changelog=True."""
    dest = DriftCheckFakeDestination(body=OTHER_BODY)
    adapter = _forgejo(dest)

    result = await ReconcileHandler.check_release_body(
        adapter, REPO, TAG, release_notes=NOTES,
    )

    assert result is not None
    assert result.empty is False
    assert result.missing_changelog is True
    assert dest.writes() == []


@pytest.mark.asyncio
async def test_drift_check_returns_none_when_body_ok():
    """Criterion 2: a body that contains the CHANGELOG returns None (no drift)."""
    dest = DriftCheckFakeDestination(body=FULL_BODY)
    adapter = _forgejo(dest)

    result = await ReconcileHandler.check_release_body(
        adapter, REPO, TAG, release_notes=NOTES,
    )

    assert result is None
    assert dest.writes() == []


@pytest.mark.asyncio
async def test_drift_check_returns_none_when_no_release():
    """Criterion 2: a tag without a release returns None."""
    dest = DriftCheckFakeDestination(has_release=False)
    adapter = _forgejo(dest)

    result = await ReconcileHandler.check_release_body(
        adapter, REPO, TAG, release_notes=NOTES,
    )

    assert result is None
    assert dest.writes() == []


@pytest.mark.asyncio
async def test_drift_check_returns_none_when_empty_repo():
    """Criterion 2: an empty repo string returns None without any calls."""
    dest = DriftCheckFakeDestination(body="")
    adapter = _forgejo(dest)

    result = await ReconcileHandler.check_release_body(
        adapter, "", TAG, release_notes=NOTES,
    )

    assert result is None
    assert dest.writes() == []


# ---------------------------------------------------------------------------
# F7: check_release_body with empty release_notes must fail closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drift_check_empty_release_notes_raises_valueerror():
    """F7 mutation guard: check_release_body with an empty release_notes must
    raise ValueError, not silently skip the CHANGELOG comparison. Deleting the
    guard must cause this test to fail.

    A non-empty body without the expected CHANGELOG section comparison is
    misleading — the caller asked for a check but got a silent success.
    Failing closed forces the caller to supply the expected notes."""
    dest = DriftCheckFakeDestination(body=OTHER_BODY)
    adapter = _forgejo(dest)

    with pytest.raises(ValueError, match="release_notes is required"):
        await ReconcileHandler.check_release_body(
            adapter, REPO, TAG, release_notes="",
        )

    assert dest.writes() == []


# ---------------------------------------------------------------------------
# F6: MissingCredentialError destination must have a body_states entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_credential_destination_has_body_states_entry(tmp_path):
    """F6 mutation guard: a destination that fails with MissingCredentialError
    must have a body_states entry. Deleting the body_states line from the
    MissingCredentialError branch must cause this test to fail.

    Every destination gets a body_states entry — criterion 1."""
    from ops_engine.adapters.factory import Credential, MissingCredentialError

    art = tmp_path / "dist"
    art.mkdir(exist_ok=True)
    for name in FILES:
        (art / name).write_bytes(name.encode())

    # Use a real Forgejo adapter with no mock transport — but supply
    # credentials={} so that MissingCredentialError is raised before any
    # network call.
    report = await ReleaseHandler.publish_release(
        _config(forge="forgejo"), REPO, TAG, NOTES, art,
        credentials={}, skip_existing=True,
        release_body=FULL_BODY,
    )

    label = f"forgejo:{REPO}"
    assert label in report.failed
    state = report.body_states.get(label, "")
    assert state.startswith("failed:"), (
        f"MissingCredentialError destination missing from body_states: {report.body_states}"
    )


# ---------------------------------------------------------------------------
# F4: mutation guard — to_dict() serialization of body_states
# ---------------------------------------------------------------------------


def test_publication_report_to_dict_includes_body_states():
    """F4 mutation guard: deleting the 'body_states' serialization line from
    PublicationReport.to_dict() must cause this test to fail.

    The serialized form is consumed downstream (workflow run summary), so the
    key must be present in to_dict() output."""
    report = PublicationReport(
        repo=REPO,
        tag_name=TAG,
        published=[f"forgejo:{REPO}"],
        body_states={f"forgejo:{REPO}": "written"},
    )
    d = report.to_dict()
    assert "body_states" in d, (
        "to_dict() is missing 'body_states' key — "
        "the downstream consumer sees no body state"
    )
    assert d["body_states"] == {f"forgejo:{REPO}": "written"}


def test_publication_report_to_dict_includes_body_states_when_empty():
    """F4 mutation guard: even an empty body_states dict must be serialized."""
    report = PublicationReport(repo=REPO, tag_name=TAG)
    d = report.to_dict()
    assert "body_states" in d, (
        "to_dict() is missing 'body_states' key — "
        "the downstream consumer sees no body state"
    )
    assert d["body_states"] == {}


# ---------------------------------------------------------------------------
# F3: mutation guard — workflow run-summary print of body_states
# ---------------------------------------------------------------------------


def test_workflow_run_summary_prints_body_states():
    """F3 mutation guard: deleting the print line for body_states from the
    shipped workflow must cause this test to fail.

    Reads the forgejo-release.yml shipped in the repository and asserts the
    exact line that prints body_states exists. If the line is deleted,
    reworded, or renamed, this test goes red."""
    import pathlib
    workflow = (
        pathlib.Path(__file__).resolve().parents[1]
        / ".forgejo" / "workflows" / "forgejo-release.yml"
    )
    lines = workflow.read_text(encoding="utf-8").splitlines()
    # The line must print body_states verbatim.  Searching for the exact
    # fragment is deliberate: a different attribute name or a print of a
    # different object would not match.
    has_line = any(
        "body_states" in line and "report.body_states" in line
        for line in lines
    )
    assert has_line, (
        "forgejo-release.yml is missing the body_states print line — "
        "the run summary must list body state per destination"
    )
# ---------------------------------------------------------------------------
# F5: test drives scripts/release-body-drift.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_body_drift_script_exits_zero_when_no_drift():
    """F5: the script exits 0 when the body is correct."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "release_body_drift",
        "scripts/release-body-drift.py",
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    dest = DriftCheckFakeDestination(body=FULL_BODY)
    adapter = _forgejo(dest)

    # Monkey-patch _build_adapter to return our fake adapter.
    script._build_adapter = lambda args: adapter

    exit_code = await script.main([
        "--repo", REPO, "--tag", TAG, "--forge", "forgejo",
        "--release-notes", NOTES,
    ])
    assert exit_code == 0, f"expected exit 0, got {exit_code}"
    assert dest.writes() == []


@pytest.mark.asyncio
async def test_release_body_drift_script_exits_nonzero_on_empty_body():
    """F5: the script exits 1 when the body is empty."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "release_body_drift2",
        "scripts/release-body-drift.py",
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    dest = DriftCheckFakeDestination(body="")
    adapter = _forgejo(dest)
    script._build_adapter = lambda args: adapter

    exit_code = await script.main([
        "--repo", REPO, "--tag", TAG, "--forge", "forgejo",
        "--release-notes", NOTES,
    ])
    assert exit_code == 1, f"expected exit 1 for empty body, got {exit_code}"
    assert dest.writes() == []


@pytest.mark.asyncio
async def test_release_body_drift_script_exits_nonzero_on_missing_changelog():
    """F5: the script exits 1 when the body lacks the CHANGELOG section."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "release_body_drift3",
        "scripts/release-body-drift.py",
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    dest = DriftCheckFakeDestination(body=OTHER_BODY)
    adapter = _forgejo(dest)
    script._build_adapter = lambda args: adapter

    exit_code = await script.main([
        "--repo", REPO, "--tag", TAG, "--forge", "forgejo",
        "--release-notes", NOTES,
    ])
    assert exit_code == 1, f"expected exit 1 for missing changelog, got {exit_code}"
    assert dest.writes() == []
