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
           Creating a second release for a tag answers 422 (LVC-299 records the
           422 only, not the body). The code ``errors[].code == "already_exists"``
           is NOT confirmed by a measurement; it is assumed from GitHub's
           documented error shape (the reference documents 422 only as generic
           validation_failed). An unexpected code stays a failure; a body
           without codes is settled by a lookup.

The fake destination here is an ``httpx`` transport behind the real adapters, so
the adapters' own request pipeline runs; only the socket is replaced.

REA-011 (3.7.0): a duplicate answer on create continues with the existing
object whether or not the caller asked for the pre-flight check. A create call
answering 409 (Forgejo) or 422 (GitHub) resolves the destination's release by
tag, for any state, and the same existing-release judgement runs. The audit
line of that resolution names the destination, the status of the duplicate
answer and the existing release id; a second delivery of the same event
writes nothing.
"""

import httpx
import pytest

from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
from ops_engine.adapters.github_adapter import GithubAdapter
from ops_engine.config_loader import OpsEngineConfig
from ops_engine.modules.release import ReleaseHandler

REPO = "exampleorg/engine"
TAG = "v9.9.9"
TITLE = "engine v9.9.9"  # the default name template rendered for REPO and TAG


def _config(forge: str) -> OpsEngineConfig:
    return OpsEngineConfig.load(
        {
            "orgs": {
                "exampleorg": {
                    "repositories": {
                        "engine": {
                            "destinations": [
                                {"forge": forge, "repo": REPO, "role": "release", "credential_variable": "FORGEJO_TOKEN"}
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

    def writes(self) -> list[tuple[str, str]]:
        """Every call that would change the destination, the create attempt
        itself aside (it is the call whose duplicate answer is absorbed)."""
        return [
            (m, p) for m, p in self.calls
            if m != "GET" and not (m == "POST" and p.endswith("/releases"))
        ]


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


async def _publish(forge: str, adapter, tmp_path, *, skip_existing: bool = True):
    art = tmp_path / "dist"
    art.mkdir(exist_ok=True)
    (art / "a.whl").write_bytes(b"x")
    audit: list[str] = []
    report = await ReleaseHandler.publish_release(
        _config(forge), REPO, TAG, "notes", art,
        adapters=[adapter], skip_existing=skip_existing, audit=audit,
    )
    return report, audit


# --- RED PROOF: by-tag 404, create 409 / 422 ---------------------------------


@pytest.mark.asyncio
async def test_forgejo_create_409_reports_destination_as_holding_the_release(tmp_path):
    dest = FakeDestination(409, {"message": "Release is has no Tag"})
    report, audit = await _publish("forgejo", _forgejo(dest), tmp_path)
    assert report.skipped and not report.failed and not report.published
    assert not report.partial
    # The token cannot see the release (by-tag 404, empty list), so its id is
    # unknown; the duplicate answer itself is still named.
    assert audit == [
        f"release-publish tag={TAG} dest={report.skipped[0]} result=skipped "
        "http=409 existing_release_id=unknown"
    ]


@pytest.mark.asyncio
async def test_github_create_422_already_exists_reports_destination_as_holding_the_release(tmp_path):
    dest = FakeDestination(422, GITHUB_ALREADY_EXISTS)
    report, audit = await _publish("github", _github(dest), tmp_path)
    assert report.skipped and not report.failed and not report.published
    assert audit == [
        f"release-publish tag={TAG} dest={report.skipped[0]} result=skipped "
        "http=422 existing_release_id=unknown"
    ]


@pytest.mark.asyncio
async def test_github_bare_422_is_checked_before_it_is_called_a_duplicate(tmp_path):
    # A 422 without the (assumed, unmeasured) already_exists code is not proof
    # of a duplicate: with nothing listed for the tag it stays a failure.
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
    dest = FakeDestination(status, body, listed=[{"tag_name": TAG, "draft": True, "name": TITLE,
                                        "assets": [{"name": "a.whl"}]}])
    report, _ = await _publish(forge, make(dest), tmp_path)
    assert report.skipped and not report.failed
    assert dest.creates() == 0


# --- OE-039 dispatch 2: an incomplete existing release, and 409/422 detail ----

RELEASE_ID = 4711


class RecordingNotifications:
    """Capture the body ``_notify_partial`` hands to the notification channel."""

    def __init__(self, monkeypatch):
        self.bodies: list[str] = []

        async def fake_process_event(event, config, context=None):
            self.bodies.append((context or {}).get("body", ""))

        monkeypatch.setattr(
            "ops_engine.modules.release.NotificationHandler.process_event",
            fake_process_event,
        )


def _notifying_config(forge: str) -> OpsEngineConfig:
    return OpsEngineConfig.load(
        {
            "orgs": {
                "exampleorg": {
                    "repositories": {
                        "engine": {
                            "destinations": [
                                {"forge": forge, "repo": REPO, "role": "release", "credential_variable": "FORGEJO_TOKEN"}
                            ],
                            "notifications": {
                                "enabled": True,
                                "channels": [{"type": "slack", "url": "https://hook.invalid/x"}],
                            },
                        }
                    }
                }
            }
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("make,forge", [(_forgejo, "forgejo"), (_github, "github")])
async def test_existing_release_without_assets_is_incomplete_and_names_the_files(
    make, forge, tmp_path
):
    dest = FakeDestination(
        409, listed=[{"id": RELEASE_ID, "tag_name": TAG, "name": TITLE, "assets": []}]
    )
    report, audit = await _publish(forge, make(dest), tmp_path)
    assert report.partial and report.failed and not report.skipped
    assert dest.creates() == 0
    assert len(audit) == 1
    assert "result=incomplete" in audit[0]
    assert "missing assets: a.whl" in audit[0]
    assert f"existing_release_id={RELEASE_ID}" in audit[0]


@pytest.mark.asyncio
async def test_existing_release_with_another_title_is_incomplete(tmp_path):
    dest = FakeDestination(
        409,
        listed=[{"id": RELEASE_ID, "tag_name": TAG, "name": "something else",
                 "assets": [{"name": "a.whl"}]}],
    )
    report, audit = await _publish("forgejo", _forgejo(dest), tmp_path)
    assert report.partial and not report.skipped
    assert "title is 'something else'" in audit[0] and repr(TITLE) in audit[0]
    assert "missing assets" not in audit[0]


@pytest.mark.asyncio
async def test_incomplete_existing_release_reaches_the_notification(tmp_path, monkeypatch):
    sink = RecordingNotifications(monkeypatch)
    dest = FakeDestination(
        409, listed=[{"id": RELEASE_ID, "tag_name": TAG, "name": TITLE, "assets": []}]
    )
    report, _ = await _publish("forgejo", _forgejo(dest), tmp_path)
    from ops_engine.modules.release import _notify_partial

    await _notify_partial(_notifying_config("forgejo"), REPO, TAG, report)
    assert len(sink.bodies) == 1
    assert "incomplete existing release" in sink.bodies[0]
    assert "a.whl" in sink.bodies[0]
    assert f"existing_release_id={RELEASE_ID}" in sink.bodies[0]


@pytest.mark.asyncio
async def test_create_409_with_no_visible_release_is_still_skipped(tmp_path):
    # By-tag 404 and an empty list (the token cannot see drafts): the create
    # answers 409. The follow-up lookup also finds nothing, so the destination
    # is skipped as before; with the release visible it is checked like any other.
    dest = FakeDestination(409)
    report, audit = await _publish("forgejo", _forgejo(dest), tmp_path)
    assert report.skipped and "incomplete" not in audit[0]


@pytest.mark.asyncio
async def test_a_422_that_is_not_a_duplicate_failure_names_http_status_and_release_id(
    tmp_path, monkeypatch
):
    # A 422 whose codes say something other than "already exists" is not a
    # duplicate answer to continue from: it stays a failure, and the audit line
    # and the notification name the status and the id of the release the
    # destination holds.
    sink = RecordingNotifications(monkeypatch)
    dest = FakeDestination(
        422,
        {"message": "Validation Failed",
         "errors": [{"code": "invalid", "field": "target_commitish"}]},
        listed=[{"id": RELEASE_ID, "tag_name": TAG, "name": TITLE}],
    )
    audit: list[str] = []
    art = tmp_path / "dist"
    art.mkdir()
    (art / "a.whl").write_bytes(b"x")
    report = await ReleaseHandler.publish_release(
        _config("github"), REPO, TAG, "notes", art, adapters=[_github(dest)], audit=audit,
    )
    assert report.partial
    expected = f"http=422 existing_release_id={RELEASE_ID}"
    assert audit == [
        f"release-publish tag={TAG} dest={report.failed[0]} result=failed {expected}"
    ]
    from ops_engine.modules.release import _notify_partial

    await _notify_partial(_notifying_config("github"), REPO, TAG, report)
    assert expected in sink.bodies[0]


@pytest.mark.asyncio
async def test_a_failure_that_is_not_409_or_422_carries_no_status_detail(tmp_path):
    dest = FakeDestination(500)
    audit: list[str] = []
    art = tmp_path / "dist"
    art.mkdir()
    (art / "a.whl").write_bytes(b"x")
    report = await ReleaseHandler.publish_release(
        _config("forgejo"), REPO, TAG, "notes", art, adapters=[_forgejo(dest)], audit=audit,
    )
    assert report.partial
    assert audit == [f"release-publish tag={TAG} dest={report.failed[0]} result=failed"]


# --- REA-011 criterion 1: a duplicate answer continues, asked or not ----------

DUPLICATE_ANSWERS = [
    ("forgejo", _forgejo, 409),
    ("github", _github, 422),
]


def _github_duplicate_body() -> dict:
    return dict(GITHUB_ALREADY_EXISTS)


def _duplicate_body(forge: str) -> dict | None:
    return _github_duplicate_body() if forge == "github" else None


def _present(*, complete: bool) -> list[dict]:
    """The release the destination holds, as the by-tag-404 list shows it.

    ``complete``: the gated title and the build's file. Otherwise the title is
    another one and no file is attached, which is an incomplete release.
    """
    return [{
        "id": RELEASE_ID,
        "tag_name": TAG,
        "name": TITLE if complete else "something else",
        "assets": [{"name": "a.whl"}] if complete else [],
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize("forge,make,status", DUPLICATE_ANSWERS)
async def test_duplicate_answer_continues_with_the_existing_object_without_the_pre_flight_check(
    forge, make, status, tmp_path
):
    # RED PROOF at base 2f5c255: skip_existing is not given, so the 409/422 is
    # not absorbed, the destination lands in ``failed`` and the workflow ends in
    # PartialPublicationError. The duplicate answer must continue with the
    # release the destination already holds, whatever state it is in.
    dest = FakeDestination(
        status, _duplicate_body(forge), listed=_present(complete=True)
    )
    report, audit = await _publish(forge, make(dest), tmp_path, skip_existing=False)
    assert not report.failed and not report.published
    assert report.skipped == [f"{forge}:{REPO}"]
    assert dest.creates() == 1  # the create call whose duplicate answer was absorbed
    assert dest.writes() == []
    assert audit == [
        f"release-publish tag={TAG} dest={report.skipped[0]} result=skipped "
        f"http={status} existing_release_id={RELEASE_ID}"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("forge,make,status", DUPLICATE_ANSWERS)
async def test_duplicate_answer_with_an_unseen_release_is_not_a_failure(
    forge, make, status, tmp_path
):
    # By-tag 404 and an empty list: the token cannot see the release at all.
    # The duplicate answer itself is the proof that the destination holds it;
    # the destination is skipped, its id is named as unknown.
    dest = FakeDestination(status, _duplicate_body(forge))
    report, audit = await _publish(forge, make(dest), tmp_path, skip_existing=False)
    assert not report.failed and not report.published
    assert report.skipped == [f"{forge}:{REPO}"]
    assert dest.writes() == []
    assert audit == [
        f"release-publish tag={TAG} dest={report.skipped[0]} result=skipped "
        f"http={status} existing_release_id=unknown"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("forge,make,status", DUPLICATE_ANSWERS)
async def test_duplicate_answer_with_an_incomplete_release_names_status_and_id(
    forge, make, status, tmp_path
):
    # The pre-flight check answered no (by-tag 404, and the list would have
    # said yes only later); the create answered a duplicate; the release the
    # destination holds is incomplete. It continues with that object and is
    # judged as such, with the duplicate answer named.
    dest = FakeDestination(
        status, _duplicate_body(forge), listed=_present(complete=False)
    )
    report, audit = await _publish(forge, make(dest), tmp_path, skip_existing=False)
    assert report.partial and report.failed == [f"{forge}:{REPO}"]
    assert not report.skipped
    assert dest.writes() == []
    assert len(audit) == 1
    assert "result=incomplete" in audit[0]
    assert f"http={status} existing_release_id={RELEASE_ID}" in audit[0]
    assert "title is 'something else'" in audit[0]


@pytest.mark.asyncio
async def test_duplicate_answer_that_is_not_a_duplicate_still_fails(tmp_path):
    # GitHub answers 422 for any validation failure; only the duplicate answer
    # continues. Nothing is listed for the tag, so this one stays a failure.
    dest = FakeDestination(
        422, {"message": "Validation Failed",
              "errors": [{"code": "invalid", "field": "target_commitish"}]}
    )
    report, audit = await _publish("github", _github(dest), tmp_path, skip_existing=False)
    assert report.failed == [f"github:{REPO}"]
    assert not report.skipped
    assert dest.writes() == []
    assert "result=failed" in audit[0] and "incomplete" not in audit[0]


# --- REA-011 criterion 2: the audit line names destination, status and id -----


@pytest.mark.asyncio
@pytest.mark.parametrize("forge,make,status", DUPLICATE_ANSWERS)
async def test_audit_line_of_a_duplicate_answer_names_destination_status_and_id(
    forge, make, status, tmp_path
):
    dest = FakeDestination(
        status, _duplicate_body(forge), listed=_present(complete=True)
    )
    report, audit = await _publish(forge, make(dest), tmp_path, skip_existing=False)
    assert len(audit) == 1
    assert audit[0] == (
        f"release-publish tag={TAG} dest={forge}:{REPO} result=skipped "
        f"http={status} existing_release_id={RELEASE_ID}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("forge,make,status", DUPLICATE_ANSWERS)
async def test_the_pre_flight_skip_carries_no_duplicate_detail(
    forge, make, status, tmp_path
):
    # A release the pre-flight lookup finds is not a duplicate answer: its
    # audit line keeps the shape every caller of publish_release relies on.
    dest = FakeDestination(status, _duplicate_body(forge), listed=_present(complete=True))
    report, audit = await _publish(forge, make(dest), tmp_path, skip_existing=True)
    assert report.skipped == [f"{forge}:{REPO}"]
    assert dest.creates() == 0
    assert audit == [
        f"release-publish tag={TAG} dest={report.skipped[0]} result=skipped"
    ]


# --- REA-011 criterion 3: redelivery of the same event writes nothing --------


@pytest.mark.asyncio
@pytest.mark.parametrize("forge,make,status", DUPLICATE_ANSWERS)
async def test_redelivery_after_a_duplicate_answer_writes_nothing(
    forge, make, status, tmp_path
):
    # First delivery: the create answers a duplicate and the existing release is
    # continued from, nothing written. The redelivery of the same event finds
    # the release and skips it: no second create, no title, no upload.
    dest = FakeDestination(
        status, _duplicate_body(forge), listed=_present(complete=True)
    )
    adapter = make(dest)
    first, _ = await _publish(forge, adapter, tmp_path, skip_existing=False)
    second, second_audit = await _publish(forge, adapter, tmp_path)

    assert first.skipped == [f"{forge}:{REPO}"]
    assert second.skipped == [f"{forge}:{REPO}"]
    assert second.failed == [] and second.published == []
    assert dest.creates() == 1  # the redelivery did not post again
    assert dest.writes() == []
    assert second_audit == [
        f"release-publish tag={TAG} dest={second.skipped[0]} result=skipped"
    ]
