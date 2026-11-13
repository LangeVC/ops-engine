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
from ops_engine.modules.release import ReleaseHandler

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
