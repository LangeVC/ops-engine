"""OE-039: a destination that already holds the release is recognised, whatever
its state, so publishing the same tag twice is a no-op for that destination.

Source of the forge facts (read 2026-11-03, not assumed):

  Forgejo  GET /repos/{o}/{r}/releases/tags/{tag}: Forgejo 16 / Gitea 1.22
           ``routers/api/v1/repo/release_tags.go`` GetReleaseByTag answers 404 only
           for a missing row or a bare git tag (``release.IsTag``); it has no
           draft filter. ``CreateRelease`` (``release.go``) answers 409 when a
           release row for the tag exists. GET /releases lists drafts for a
           token with write access (``IncludeDrafts``), filter ``draft`` optional
           (swagger: "if you dont have repo write access none will show").
  GitHub   GET /repos/{o}/{r}/releases/tags/{tag}: "Get a published release with
           the specified tag" (rest-api-description, repos/get-release-by-tag),
           so a DRAFT answers 404. GET /releases: "Only users with push access
           will receive listings for draft releases" (repos/list-releases).
           Creating a second release for a tag answers 422 with
           ``errors[].code == "already_exists"`` (measured in LVC-299).

The fake destination here is an ``httpx`` transport behind the real adapters, so
the adapters' own request pipeline runs; only the socket is replaced.
"""

import httpx
import pytest

from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
from ops_engine.adapters.github_adapter import GithubAdapter
from ops_engine.config_loader import OpsEngineConfig
from ops_engine.modules.release import ReleaseHandler

REPO = "exampleorg/engine"
TAG = "v9.9.9"


def _config(forge: str) -> OpsEngineConfig:
    return OpsEngineConfig.load(
        {
            "orgs": {
                "exampleorg": {
                    "repositories": {
                        "engine": {
                            "destinations": [
                                {"forge": forge, "repo": REPO, "role": "release"}
                            ]
                        }
                    }
                }
            }
        }
    )


class FakeDestination:
    """By-tag lookup answers 404, create answers ``create_status``; the list
    endpoint serves ``listed`` (releases of any state)."""

    def __init__(self, create_status: int, create_body: dict | None = None,
                 listed: list[dict] | None = None):
        self.create_status = create_status
        self.create_body = create_body if create_body is not None else {"message": "x"}
        self.listed = listed or []
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if request.method == "GET" and "/releases/tags/" in path:
            return httpx.Response(404, json={"message": "Not Found"})
        if request.method == "GET" and path.endswith("/releases"):
            page = request.url.params.get("page", "1")
            return httpx.Response(200, json=self.listed if page == "1" else [])
        if request.method == "POST" and path.endswith("/releases"):
            return httpx.Response(self.create_status, json=self.create_body)
        return httpx.Response(500, json={"message": f"unexpected {request.method} {path}"})

    def creates(self) -> int:
        return sum(1 for m, p in self.calls if m == "POST" and p.endswith("/releases"))


def _forgejo(dest: FakeDestination) -> ForgejoAdapter:
    adapter = ForgejoAdapter(base_url="https://forge.invalid", token="t", webhook_secret="s")
    adapter._client = httpx.AsyncClient(
        base_url="https://forge.invalid/api/v1", transport=httpx.MockTransport(dest.handler)
    )
    return adapter


def _github(dest: FakeDestination) -> GithubAdapter:
    adapter = GithubAdapter(token="t", webhook_secret="s")
    adapter._client = httpx.AsyncClient(
        base_url="https://api.github.invalid", transport=httpx.MockTransport(dest.handler)
    )
    return adapter


GITHUB_ALREADY_EXISTS = {
    "message": "Validation Failed",
    "errors": [{"resource": "Release", "code": "already_exists", "field": "tag_name"}],
}


async def _publish(forge: str, adapter, tmp_path):
    art = tmp_path / "dist"
    art.mkdir(exist_ok=True)
    (art / "a.whl").write_bytes(b"x")
    audit: list[str] = []
    report = await ReleaseHandler.publish_release(
        _config(forge), REPO, TAG, "notes", art,
        adapters=[adapter], skip_existing=True, audit=audit,
    )
    return report, audit


# --- RED PROOF: by-tag 404, create 409 / 422 ---------------------------------


@pytest.mark.asyncio
async def test_forgejo_create_409_reports_destination_as_holding_the_release(tmp_path):
    dest = FakeDestination(409, {"message": "Release is has no Tag"})
    report, audit = await _publish("forgejo", _forgejo(dest), tmp_path)
    assert report.skipped and not report.failed and not report.published
    assert not report.partial
    assert audit == [f"release-publish tag={TAG} dest={report.skipped[0]} result=skipped"]


@pytest.mark.asyncio
async def test_github_create_422_already_exists_reports_destination_as_holding_the_release(tmp_path):
    dest = FakeDestination(422, GITHUB_ALREADY_EXISTS)
    report, audit = await _publish("github", _github(dest), tmp_path)
    assert report.skipped and not report.failed and not report.published
    assert audit == [f"release-publish tag={TAG} dest={report.skipped[0]} result=skipped"]


@pytest.mark.asyncio
async def test_github_bare_422_is_checked_before_it_is_called_a_duplicate(tmp_path):
    # A 422 without the already_exists code is not proof of a duplicate: with
    # nothing listed for the tag it stays a failure.
    dest = FakeDestination(422, {"message": "Validation Failed",
                                 "errors": [{"code": "invalid", "field": "target_commitish"}]})
    report, _ = await _publish("github", _github(dest), tmp_path)
    assert report.failed and not report.skipped


# --- the check itself: any state, found by listing when by-tag says 404 ------


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [
    {"draft": False, "prerelease": False},
    {"draft": True, "prerelease": False},
    {"draft": False, "prerelease": True},
])
@pytest.mark.parametrize("make", [_forgejo, _github])
async def test_release_exists_true_for_any_state_when_by_tag_says_404(make, state):
    dest = FakeDestination(409, listed=[{"tag_name": "v0.0.1", **state},
                                        {"tag_name": TAG, **state}])
    assert await make(dest).release_exists(REPO, TAG) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("make", [_forgejo, _github])
async def test_release_exists_false_when_neither_lookup_finds_the_tag(make):
    dest = FakeDestination(409, listed=[{"tag_name": "v0.0.1", "draft": True}])
    assert await make(dest).release_exists(REPO, TAG) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("forge,make,status,body", [
    ("forgejo", _forgejo, 409, None),
    ("github", _github, 422, GITHUB_ALREADY_EXISTS),
])
async def test_second_publication_is_a_no_op(forge, make, status, body, tmp_path):
    # The draft exists on the destination, by-tag cannot see it, the list can:
    # the second call finds it first and never posts.
    dest = FakeDestination(status, body, listed=[{"tag_name": TAG, "draft": True}])
    report, _ = await _publish(forge, make(dest), tmp_path)
    assert report.skipped and not report.failed
    assert dest.creates() == 0
