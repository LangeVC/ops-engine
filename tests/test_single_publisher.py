"""P36-003: one publisher per repository.

Two publishers exist for a repository: the ops bot (the account the engine
publishes as) and a repository-local workflow (D35 / FAI-211). With the public
mirror back on, both would write the same release.

Criterion 1: for a tag the engine creates exactly one release; a release that
already exists from ANOTHER publisher is recorded as up to date and is neither
edited nor duplicated.
Criterion 2: the report names the publisher found (``engine`` or ``other``)
per tag.
Criterion 3 (RED PROOF): the 3.5.2 call shape turns the same pre-existing
release into a failed destination (the run goes red); the new behaviour
neither fails nor writes.

The publisher is read from the release object's own ``author.login`` — the
forge's release object carries the account that created it (Forgejo/Gitea
``Release.author``, GitHub release ``author``). The login the engine publishes
as cannot be derived from config (``ForgejoIdentity.display_name`` and
``GithubIdentity.login`` are stored org attributes, not propagated to a
resolved repository config, and the destinations carry no account), so it is a
caller-supplied input: ``publish_release(..., publisher_login=...)``. Without
it the engine cannot attribute a release and falls back to the established
pre-P36-003 behaviour, so no existing caller changes meaning.

The fake destination is an ``httpx`` transport behind the real adapters.
"""

import httpx
import pytest

from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
from ops_engine.adapters.github_adapter import GithubAdapter
from ops_engine.config_loader import OpsEngineConfig
from ops_engine.modules.release import ReleaseHandler

REPO = "langevc/ops-engine"
TAG = "v3.5.1"
# The default name template rendered for REPO and TAG.
TITLE = "ops-engine v3.5.1"
ENGINE_LOGIN = "dev.bot_langevc"
OTHER_LOGIN = "github-actions[bot]"
NOTES = "A release published for the public mirror.\n\n- One build, one release.\n"
OTHER_NOTES = "Release created by the repository workflow.\n"
FILES = ["ops_engine-3.5.1-py3-none-any.whl", "SHA256SUMS"]
RELEASE_ID = 77
FORGE_LABEL = {f"forgejo:{REPO}", f"github:{REPO}"}


def _config(forge: str) -> OpsEngineConfig:
    return OpsEngineConfig.load(
        {
            "orgs": {
                "langevc": {
                    "repositories": {
                        "ops-engine": {
                            "destinations": [
                                {"forge": forge, "repo": REPO, "role": "release"}
                            ]
                        }
                    }
                }
            }
        }
    )


def _release(*, author: str | None, **over) -> dict:
    """A forge release object. ``author`` is the creating account's login."""
    release = {
        "id": RELEASE_ID,
        "tag_name": TAG,
        "name": "Release 3.5.1",
        "body": OTHER_NOTES,
        "draft": False,
        "prerelease": False,
        "assets": [{"name": "release.zip"}],
        "tarball_url": f"https://forge.invalid/{REPO}/archive/{TAG}.tar.gz",
        "zipball_url": f"https://forge.invalid/{REPO}/archive/{TAG}.zip",
    }
    if author is not None:
        release["author"] = {"login": author}
    release.update(over)
    return release


class FakeDestination:
    """Serves one release by tag; records every write."""

    def __init__(self, release: dict | None = None, create_status: int | None = None):
        self.release = release
        self.create_status = create_status
        self.calls: list[tuple[str, str]] = []
        self.patches: list[dict] = []
        self.uploads: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        import json

        path = request.url.path
        self.calls.append((request.method, path))
        if request.method == "GET" and "/releases/tags/" in path:
            if self.release is None:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json=self.release)
        if request.method == "GET" and path.endswith("/releases"):
            rows = [] if self.release is None else [self.release]
            return httpx.Response(200, json=rows)
        if request.method == "PATCH" and path.endswith(f"/releases/{RELEASE_ID}"):
            body = json.loads(request.content)
            self.patches.append(body)
            self.release = {**self.release, **body}
            return httpx.Response(200, json=self.release)
        if request.method == "POST" and path.endswith(f"/releases/{RELEASE_ID}/assets"):
            name = request.url.params.get("name")
            self.uploads.append(name)
            return httpx.Response(201, json={"name": name})
        if request.method == "POST" and path.endswith("/releases"):
            if self.create_status:
                return httpx.Response(self.create_status, json={"message": "x"})
            return httpx.Response(
                201, json={"id": RELEASE_ID, "tag_name": TAG, "name": TITLE}
            )
        return httpx.Response(500, json={"message": f"unexpected {request.method} {path}"})

    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p in self.calls if m != "GET"]

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


async def _publish(forge: str, adapter, tmp_path, **over):
    art = tmp_path / "dist"
    art.mkdir(exist_ok=True)
    for name in FILES:
        (art / name).write_bytes(name.encode())
    audit: list[str] = []
    report = await ReleaseHandler.publish_release(
        _config(forge), REPO, TAG, NOTES, art,
        adapters=[adapter], skip_existing=True, audit=audit, **over,
    )
    return report, audit


@pytest.mark.parametrize("forge,make", [("forgejo", _forgejo), ("github", _github)])
@pytest.mark.asyncio
async def test_another_publishers_release_is_up_to_date_and_untouched(forge, make, tmp_path):
    """Criterion 1: a pre-existing release from another publisher is recorded
    as up to date. Not edited (no title PATCH, no asset upload) and not
    duplicated (no create)."""
    dest = FakeDestination(_release(author=OTHER_LOGIN))
    report, audit = await _publish(
        forge, make(dest), tmp_path, publisher_login=ENGINE_LOGIN
    )

    label = f"{forge}:{REPO}"
    assert dest.writes() == []
    assert report.up_to_date == [label]
    assert report.failed == [] and report.failure_details == {}
    assert report.published == [] and report.supplemented == [] and report.skipped == []
    assert audit == [
        f"release-publish tag={TAG} dest={label} result=up-to-date publisher=other"
    ]


@pytest.mark.parametrize("forge,make", [("forgejo", _forgejo), ("github", _github)])
@pytest.mark.asyncio
async def test_the_report_names_the_other_publisher_per_tag(forge, make, tmp_path):
    """Criterion 2: the report names the publisher found for the tag."""
    dest = FakeDestination(_release(author=OTHER_LOGIN))
    report, _ = await _publish(forge, make(dest), tmp_path, publisher_login=ENGINE_LOGIN)

    assert report.publisher == "other"
    assert report.tag_name == TAG


@pytest.mark.parametrize("forge,make", [("forgejo", _forgejo), ("github", _github)])
@pytest.mark.asyncio
async def test_the_engine_creates_exactly_one_release_per_tag(forge, make, tmp_path):
    """Criterion 1: when nothing exists the engine creates exactly one release
    for the tag and names itself as the publisher."""
    dest = FakeDestination()
    report, _ = await _publish(forge, make(dest), tmp_path, publisher_login=ENGINE_LOGIN)

    label = f"{forge}:{REPO}"
    assert dest.creates() == 1
    assert sorted(dest.uploads) == sorted(FILES)
    assert report.published == [label]
    assert report.publisher == "engine"
    assert report.failed == [] and report.up_to_date == []


@pytest.mark.asyncio
async def test_the_engines_own_earlier_release_is_supplemented_and_not_duplicated(tmp_path):
    """Criterion 1: a release attributable to the engine itself stays the
    engine's to finish — it is supplemented, never re-created, and the tag is
    named ``engine``, not ``other``."""
    dest = FakeDestination(
        _release(author=ENGINE_LOGIN, name=TITLE, body="\n" + NOTES + "\n\n", assets=[])
    )
    report, audit = await _publish(
        "forgejo", _forgejo(dest), tmp_path, publisher_login=ENGINE_LOGIN
    )

    assert dest.patches == []  # the title already is the gated one
    assert sorted(dest.uploads) == sorted(FILES)
    assert dest.creates() == 0
    assert len(report.supplemented) == 1 and report.published == []
    assert report.up_to_date == [] and report.failed == []
    assert report.publisher == "engine"
    assert "result=supplemented" in audit[0]


@pytest.mark.asyncio
async def test_a_release_without_an_author_is_named_other_and_never_engine(tmp_path):
    """Criterion 2: without an author the publisher cannot be proven to be the
    engine, so the report says ``other`` — never a false ``engine``."""
    complete = _release(author=None, name=TITLE, body=NOTES, assets=[{"name": n} for n in FILES])
    dest = FakeDestination(complete)
    report, _ = await _publish(
        "forgejo", _forgejo(dest), tmp_path, publisher_login=ENGINE_LOGIN
    )

    assert dest.writes() == []
    assert report.publisher == "other"
    assert report.failed == [] and len(report.skipped) == 1


@pytest.mark.parametrize(
    "forge,make,status", [("forgejo", _forgejo, 409), ("github", _github, 422)]
)
@pytest.mark.asyncio
async def test_a_duplicate_answered_for_another_publishers_release_is_up_to_date(
    forge, make, status, tmp_path
):
    """Criterion 1: the by-tag lookup cannot see a draft, the create answers a
    duplicate; when the release belongs to another publisher it is recorded as
    up to date and no second create is attempted."""
    dest = FakeDestination(_release(author=OTHER_LOGIN), create_status=status)
    # by-tag 404 until the create has been attempted, then the release shows.
    calls = dest.calls
    real = dest.handler
    created = {"attempted": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and not created["attempted"]:
            calls.append((request.method, request.url.path))
            if "/releases/tags/" in request.url.path:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path.endswith("/releases"):
            created["attempted"] = True
        return real(request)

    adapter = make(dest)
    adapter._client = httpx.AsyncClient(
        base_url=(
            "https://forge.invalid/api/v1" if forge == "forgejo" else "https://api.github.invalid"
        ),
        transport=httpx.MockTransport(handler),
    )
    report, audit = await _publish(forge, adapter, tmp_path, publisher_login=ENGINE_LOGIN)

    label = f"{forge}:{REPO}"
    assert dest.creates() == 1  # the duplicate answer was not retried
    assert dest.patches == [] and dest.uploads == []
    assert report.up_to_date == [label]
    assert report.failed == [] and report.published == []
    assert report.publisher == "other"
    assert audit[0].endswith("result=up-to-date publisher=other")


@pytest.mark.asyncio
async def test_legacy_call_shape_still_fails_a_foreign_release_and_the_new_one_does_not(
    tmp_path,
):
    """Criterion 3 (RED PROOF): with the same pre-existing release the 3.5.2
    call shape (``skip_existing`` only, no publisher input) records the
    destination as failed — the run goes red — while the new shape records it
    as up to date and writes nothing."""
    legacy = FakeDestination(_release(author=OTHER_LOGIN))
    legacy_report, legacy_audit = await _publish("forgejo", _forgejo(legacy), tmp_path)
    assert legacy.writes() == []
    assert legacy_report.failed == [f"forgejo:{REPO}"]
    assert "notes differ" in legacy_report.failure_details[f"forgejo:{REPO}"]
    assert "result=incomplete" in legacy_audit[0]

    new = FakeDestination(_release(author=OTHER_LOGIN))
    new_report, new_audit = await _publish(
        "forgejo", _forgejo(new), tmp_path, publisher_login=ENGINE_LOGIN
    )
    assert new.writes() == []
    assert new_report.failed == []
    assert new_report.up_to_date == [f"forgejo:{REPO}"]
    assert "result=up-to-date" in new_audit[0]
