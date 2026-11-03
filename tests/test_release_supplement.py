"""OE-042a: an existing release that the ops bot created ahead of the pipeline is
supplemented: its title is corrected to the gated title and exactly the missing
files are uploaded. Nothing is re-created or deleted.

The double reproduces the real bot release of v3.5.1. The real object was NOT
available (the owner deleted the release before it was saved), so the fields
below are RECONSTRUCTED from the owner's screenshot, not measured: title in
small letters, no attached file, the release text of 3.5.1. The key names follow
the forge's own release object.

Forge facts (read 2026-11-03 from the forges' machine-readable references):

  Forgejo  swagger of Forgejo 16 (Gitea 1.22 API): ``PATCH
           /repos/{owner}/{repo}/releases/{id}`` takes ``EditReleaseOption``
           with ``name`` (x-go-name Title) and ``body``. The Release object has
           ``body``, ``assets``, and the source archives as separate
           ``tarball_url`` / ``zipball_url`` fields, so the archives are not in
           ``assets``.
  GitHub   rest-api-description (api.github.com.json) ``PATCH
           /repos/{owner}/{repo}/releases/{release_id}`` ("Update a release")
           takes ``name`` ("The name of the release."). The release schema has
           ``body`` (nullable), ``assets`` (release-asset items) and the source
           archives as ``tarball_url`` / ``zipball_url``, not in ``assets``.
  Both     ``find_release`` returns the forge's own release object (the by-tag
           JSON, or the matching row of the release list), so ``body`` and
           ``assets`` are present without a change to ``find_release``.

The fake destination is an ``httpx`` transport behind the real adapters.
"""

import httpx
import pytest

from ops_engine.adapters.base import ForgeAdapter
from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
from ops_engine.adapters.github_adapter import GithubAdapter
from ops_engine.config_loader import OpsEngineConfig
from ops_engine.modules.release import ReleaseHandler

REPO = "langevc/ops-engine"
TAG = "v3.5.1"
BOT_TITLE = "ops-engine v3.5.1"
GATED_TITLE = "Ops Engine v3.5.1"
TEMPLATE = "Ops Engine {tag_name}"
NOTES = (
    "A release that already existed on a destination could be reported as a "
    "failure.\n\n### Existing releases are recognised\n\n- Left alone.\n"
)
FILES = ["ops_engine-3.5.1-py3-none-any.whl", "ops_engine-3.5.1.tar.gz",
         "SHA256SUMS", "SHA256SUMS.sig"]
RELEASE_ID = 77


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


def _bot_release(**over) -> dict:
    """The bot release: lowercase title, the notes of this build, no attached
    file (the source archives are tarball_url / zipball_url, not assets)."""
    release = {
        "id": RELEASE_ID,
        "tag_name": TAG,
        "name": BOT_TITLE,
        "body": NOTES,
        "draft": False,
        "prerelease": False,
        "assets": [],
        "tarball_url": f"https://forge.invalid/{REPO}/archive/{TAG}.tar.gz",
        "zipball_url": f"https://forge.invalid/{REPO}/archive/{TAG}.zip",
    }
    release.update(over)
    return release


class FakeDestination:
    """Serves one existing release by tag; records every write."""

    def __init__(self, release: dict, create_status: int | None = None):
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
            return httpx.Response(200, json=self.release)
        if request.method == "PATCH" and path.endswith(f"/releases/{RELEASE_ID}"):
            body = json.loads(request.content)
            self.patches.append(body)
            self.release = {**self.release, **body}
            return httpx.Response(200, json=self.release)
        if request.method == "POST" and path.endswith(f"/releases/{RELEASE_ID}/assets"):
            name = request.url.params.get("name")
            self.uploads.append(name)
            self.release["assets"] = [*self.release["assets"], {"name": name}]
            return httpx.Response(201, json={"name": name})
        if request.method == "POST" and path.endswith("/releases"):
            return httpx.Response(self.create_status or 500, json={"message": "x"})
        return httpx.Response(500, json={"message": f"unexpected {request.method} {path}"})

    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p in self.calls if m != "GET"]


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


async def _publish(forge: str, adapter, tmp_path, notes: str = NOTES):
    art = tmp_path / "dist"
    art.mkdir(exist_ok=True)
    for name in FILES:
        (art / name).write_bytes(name.encode())
    audit: list[str] = []
    report = await ReleaseHandler.publish_release(
        _config(forge), REPO, TAG, notes, art,
        name_template=TEMPLATE, adapters=[adapter], skip_existing=True, audit=audit,
    )
    return report, audit


@pytest.mark.parametrize("forge,make", [("forgejo", _forgejo), ("github", _github)])
@pytest.mark.asyncio
async def test_bot_release_gets_gated_title_and_exactly_the_four_files(forge, make, tmp_path):
    dest = FakeDestination(_bot_release())
    report, audit = await _publish(forge, make(dest), tmp_path)

    assert dest.patches == [{"name": GATED_TITLE}]
    assert sorted(dest.uploads) == sorted(FILES)
    assert not any(m == "POST" and p.endswith("/releases") for m, p in dest.calls)
    assert not any(m == "DELETE" for m, _ in dest.calls)
    assert len(report.supplemented) == 1
    assert report.failed == []
    assert report.published == [] and report.skipped == []
    assert len(audit) == 1
    assert "result=supplemented" in audit[0]
    assert BOT_TITLE in audit[0] and GATED_TITLE in audit[0]
    for name in FILES:
        assert name in audit[0]


@pytest.mark.asyncio
async def test_files_already_attached_are_untouched(tmp_path):
    attached = [{"name": FILES[0]}, {"name": FILES[2]}]
    dest = FakeDestination(_bot_release(assets=attached))
    report, audit = await _publish("forgejo", _forgejo(dest), tmp_path)

    assert sorted(dest.uploads) == sorted([FILES[1], FILES[3]])
    assert report.failed == [] and len(report.supplemented) == 1
    assert "result=supplemented" in audit[0]


@pytest.mark.asyncio
async def test_title_differing_by_case_and_whitespace_only_is_corrected(tmp_path):
    dest = FakeDestination(_bot_release(name="  OPS   engine V3.5.1 "))
    report, _ = await _publish("forgejo", _forgejo(dest), tmp_path)

    assert dest.patches == [{"name": GATED_TITLE}]
    assert report.failed == [] and len(report.supplemented) == 1


@pytest.mark.asyncio
async def test_complete_release_with_gated_title_is_skipped_untouched(tmp_path):
    dest = FakeDestination(
        _bot_release(name=GATED_TITLE, assets=[{"name": n} for n in FILES])
    )
    report, audit = await _publish("forgejo", _forgejo(dest), tmp_path)

    assert dest.writes() == []
    assert len(report.skipped) == 1 and report.supplemented == []
    assert "result=skipped" in audit[0]


@pytest.mark.asyncio
async def test_different_notes_leave_the_release_untouched_and_fail(tmp_path):
    dest = FakeDestination(_bot_release(body="some other text"))
    report, audit = await _publish("forgejo", _forgejo(dest), tmp_path)

    assert dest.writes() == []
    assert len(report.failed) == 1 and report.supplemented == []
    assert "result=incomplete" in audit[0]


@pytest.mark.asyncio
async def test_title_differing_by_more_than_case_leaves_it_untouched_and_fails(tmp_path):
    dest = FakeDestination(_bot_release(name="Something else v3.5.1"))
    report, audit = await _publish("forgejo", _forgejo(dest), tmp_path)

    assert dest.writes() == []
    assert len(report.failed) == 1 and report.supplemented == []
    assert "title is" in report.failure_details[report.failed[0]]


@pytest.mark.asyncio
async def test_notes_are_compared_after_stripping(tmp_path):
    dest = FakeDestination(_bot_release(body="\n" + NOTES + "\n\n"))
    report, _ = await _publish("forgejo", _forgejo(dest), tmp_path)

    assert report.failed == [] and len(report.supplemented) == 1


@pytest.mark.asyncio
async def test_adapter_without_title_change_fails_the_destination_and_uploads_nothing(tmp_path):
    class NoTitleChange(ForgeAdapter):
        """Only what the supplement reads; the title call is the base default."""

        async def release_exists(self, repo_full_name, tag_name):
            return True

        async def find_release(self, repo_full_name, tag_name):
            return _bot_release()

        async def upload_release_asset(self, *a, **k):  # pragma: no cover
            raise AssertionError("must not upload when the title cannot be changed")

    NoTitleChange.__abstractmethods__ = frozenset()
    adapter = NoTitleChange()
    report, audit = await _publish("forgejo", adapter, tmp_path)

    assert len(report.failed) == 1 and report.supplemented == []
    assert "result=failed" in audit[0]


@pytest.mark.asyncio
async def test_default_update_release_title_raises_not_implemented():
    class Bare(ForgeAdapter):
        pass

    Bare.__abstractmethods__ = frozenset()
    with pytest.raises(NotImplementedError):
        await Bare().update_release_title("o/r", 1, "t")
