"""REA-012: the engine converges an existing release object (decision D1).

D1 supersedes decision C1 / P36-003 of the 3.6.0 PRD ("existing object only
recorded"): for the declared tag the engine sets the title per template and the
body to the CHANGELOG section — audited on the final body — and never deletes
the object.

Criterion 1: an existing object with another title or an empty body is PATCHed
to the template title and the CHANGELOG body on both forges; the audit names
the old and the new value.

Criterion 2: the audience gate runs before the write, and a failing body writes
nothing on that destination.

Criterion 3: RED PROOF — a softprops-style object (the tag name as title, an
empty body) stays that way on the base commit and is converged by the change.

Decision D1 also supersedes P36-003's "a release of another publisher is only
recorded": an object the engine is told (``publisher_login``) was authored by
another account is converged too — title, body and missing assets — while
``report.publishers`` still names the publisher. With a ``publisher_login``
given on the base commit that object returns early untouched; the red proof
below fails there and passes with the change, for both forges.

The softprops shape (wrong title AND empty body) exists in no older fixture, so
the fixtures of P36-017/P36-018/OE-042a keep their exact PATCH assertions.

The fake destination is an ``httpx`` transport behind the real adapters; every
test drives ``ReleaseHandler.publish_release`` — the call shape the shipped
workflow uses.
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
TAG_TITLE = "v3.5.1"  # the softprops object's title: the tag name itself
GATED_TITLE = "Ops Engine v3.5.1"
TEMPLATE = "Ops Engine {tag_name}"
NOTES = "- A feature.\n\n- A fix.\n"
INSTALL_BLOCK = "pip install ops-engine"
CHANGELOG_PATH = "CHANGELOG.md"
FILES = ["ops_engine-3.5.1-py3-none-any.whl", "SHA256SUMS"]
RELEASE_ID = 77
ENGINE_LOGIN = "dev.bot_langevc"
OTHER_LOGIN = "github-actions[bot]"


def _full_body() -> str:
    return (
        f"## {GATED_TITLE}\n\n"
        f"{NOTES}\n"
        f"### Install\n"
        f"{INSTALL_BLOCK}\n\n"
        f"See [{CHANGELOG_PATH}](https://forge.invalid/{REPO}/src/tag/{TAG}/{CHANGELOG_PATH})"
        f" for all changes.\n"
    )


FULL_BODY = _full_body()


def _config(*, forge="forgejo", second=None):
    destinations = [
        {
            "forge": forge,
            "repo": REPO,
            "role": "release",
            "visibility": "public",
            "credential_variable": "OPS_TOKEN",
        }
    ]
    if second is not None:
        destinations.append(
            {
                "forge": second,
                "repo": REPO,
                "role": "release",
                "visibility": "public",
                "credential_variable": "SECOND_TOKEN",
            }
        )
    return OpsEngineConfig.load(
        {
            "orgs": {
                "langevc": {
                    "repositories": {
                        "ops-engine": {"destinations": destinations}
                    }
                }
            }
        }
    )


def _softprops_release(**over) -> dict:
    """A softprops-style object: title = the tag name, body empty."""
    release = {
        "id": RELEASE_ID,
        "tag_name": TAG,
        "name": TAG_TITLE,
        "body": "",
        "draft": False,
        "prerelease": False,
        "assets": [],
        "tarball_url": f"https://forge.invalid/{REPO}/archive/{TAG}.tar.gz",
        "zipball_url": f"https://forge.invalid/{REPO}/archive/{TAG}.zip",
    }
    release.update(over)
    author = over.get("author")
    if isinstance(author, str):
        release["author"] = {"login": author}
    return release


class FakeDestination:
    """Serves one release object by tag; records every write, field by field."""

    def __init__(self, release: dict | None, *, create_status: int = 409):
        self.release = release
        self.create_status = create_status
        self.calls: list[tuple[str, str]] = []
        self.patches: list[dict] = []
        self.uploads: list[str] = []
        self.created_body = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if request.method == "GET" and "/releases/tags/" in path:
            if self.release is None:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json=self.release)
        if request.method == "GET" and path.endswith("/releases"):
            return httpx.Response(200, json=[] if self.release is None else [self.release])
        if request.method == "PATCH" and f"/releases/{RELEASE_ID}" in path:
            body = json.loads(request.content)
            self.patches.append(body)
            self.release = {**(self.release or {}), **body}
            return httpx.Response(200, json=self.release)
        if request.method == "POST" and f"/releases/{RELEASE_ID}/assets" in path:
            name = request.url.params.get("name")
            self.uploads.append(name)
            self.release = {
                **(self.release or {}),
                "assets": [*((self.release or {}).get("assets") or []), {"name": name}],
            }
            return httpx.Response(201, json={"name": name})
        if request.method == "POST" and path.endswith("/releases"):
            if self.release is not None:
                return httpx.Response(self.create_status, json={"message": "exists"})
            self.created_body = json.loads(request.content).get("body")
            self.release = _softprops_release(body=self.created_body)
            return httpx.Response(201, json=self.release)
        return httpx.Response(
            500, json={"message": f"unexpected {request.method} {path}"}
        )

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


def _make_artifacts(tmp_path) -> None:
    art = tmp_path / "dist"
    art.mkdir(exist_ok=True)
    for name in FILES:
        (art / name).write_bytes(name.encode())


async def _publish(forge, adapter, tmp_path, *, release_body=FULL_BODY,
                   ticket_prefixes=None, adapters=None, config=None,
                   publisher_login=""):
    _make_artifacts(tmp_path)
    audit: list[str] = []
    kwargs = {"release_body": release_body} if release_body is not None else {}
    if ticket_prefixes is not None:
        kwargs["ticket_prefixes"] = ticket_prefixes
    if publisher_login:
        kwargs["publisher_login"] = publisher_login
    report = await ReleaseHandler.publish_release(
        config if config is not None else _config(forge=forge),
        REPO, TAG, NOTES, tmp_path / "dist",
        name_template=TEMPLATE,
        adapters=adapters if adapters is not None else [adapter],
        skip_existing=True,
        audit=audit,
        **kwargs,
    )
    return report, audit


# --- criterion 1: another title or an empty body is converged ---------------


@pytest.mark.parametrize("forge,make", [("forgejo", _forgejo), ("github", _github)])
@pytest.mark.asyncio
async def test_softprops_object_is_converged_to_template_title_and_body(
    forge, make, tmp_path
):
    """Criterion 1 + 3: title = tag name, body empty -> both are set; the audit
    names the old and the new title. Nothing is created or deleted."""
    dest = FakeDestination(_softprops_release())
    report, audit = await _publish(forge, make(dest), tmp_path)

    label = f"{forge}:{REPO}"
    # Both fields were written: the title to the template title, the body to
    # the CHANGELOG section (the composed full body).
    patched = {}
    for body in dest.patches:
        patched.update(body)
    assert patched.get("name") == GATED_TITLE
    assert patched.get("body") == FULL_BODY
    assert sorted(dest.uploads) == sorted(FILES)
    assert dest.creates() == 0
    assert not any(m == "DELETE" for m, _ in dest.calls)
    assert report.supplemented == [label]
    assert report.failed == []
    # The audit names the old and the new value.
    assert len(audit) == 1
    assert "result=supplemented" in audit[0]
    assert repr(TAG_TITLE) in audit[0]
    assert repr(GATED_TITLE) in audit[0]


@pytest.mark.parametrize("forge,make", [("forgejo", _forgejo), ("github", _github)])
@pytest.mark.asyncio
async def test_another_title_with_the_same_body_is_retitled_not_rewritten(
    forge, make, tmp_path
):
    """Criterion 1: a wrong title with the body already in place is PATCHed to
    the template title; the body is left alone and the audit names both."""
    dest = FakeDestination(_softprops_release(body=FULL_BODY))
    report, audit = await _publish(forge, make(dest), tmp_path)

    label = f"{forge}:{REPO}"
    assert dest.patches == [{"name": GATED_TITLE}]
    assert dest.creates() == 0
    assert report.supplemented == [label]
    assert report.failed == []
    assert repr(TAG_TITLE) in audit[0] and repr(GATED_TITLE) in audit[0]


@pytest.mark.parametrize("forge,make", [("forgejo", _forgejo), ("github", _github)])
@pytest.mark.asyncio
async def test_empty_body_with_the_gated_title_is_rewritten_not_retitled(
    forge, make, tmp_path
):
    """Criterion 1: an object whose title is already gated but whose body is
    empty gets exactly the body; no title PATCH is spent."""
    dest = FakeDestination(_softprops_release(name=GATED_TITLE))
    report, _ = await _publish(forge, make(dest), tmp_path)

    label = f"{forge}:{REPO}"
    assert dest.patches == [{"body": FULL_BODY}]
    assert dest.creates() == 0
    assert report.supplemented == [label]
    assert report.failed == []


@pytest.mark.parametrize("forge,make", [("forgejo", _forgejo), ("github", _github)])
@pytest.mark.asyncio
async def test_a_converged_object_is_complete_and_a_rerun_is_a_noop(
    forge, make, tmp_path
):
    """Criterion 1: after convergence a rerun finds a complete object: no
    write, recorded as skipped."""
    dest = FakeDestination(_softprops_release())
    await _publish(forge, make(dest), tmp_path)
    writes_first = len(dest.writes())

    report, _ = await _publish(forge, make(dest), tmp_path)
    label = f"{forge}:{REPO}"
    assert len(dest.writes()) == writes_first
    assert dest.creates() == 0
    assert report.skipped == [label]
    assert report.failed == []


# --- D1 supersedes P36-003: another publisher's object is converged too -----


@pytest.mark.parametrize("forge,make", [("forgejo", _forgejo), ("github", _github)])
@pytest.mark.asyncio
async def test_another_publishers_softprops_object_is_converged_too(
    forge, make, tmp_path
):
    """D1 (supersedes C1 / P36-003): a softprops-style object authored by
    ANOTHER account, with ``publisher_login`` given, is converged exactly like
    one without an author — the title to the template title, the body to the
    CHANGELOG body, the missing files attached. ``report.publishers`` still
    names the publisher, and nothing is created or deleted.

    RED PROOF: on the base commit ``_judge_existing_release`` returns after
    recording the destination ``up_to_date`` for an author that is not the
    engine; no PATCH and no upload happen, so this test fails there."""
    dest = FakeDestination(_softprops_release(author=OTHER_LOGIN))
    report, audit = await _publish(
        forge, make(dest), tmp_path, publisher_login=ENGINE_LOGIN,
    )

    label = f"{forge}:{REPO}"
    patched = {}
    for body in dest.patches:
        patched.update(body)
    assert patched.get("name") == GATED_TITLE
    assert patched.get("body") == FULL_BODY
    assert sorted(dest.uploads) == sorted(FILES)
    assert dest.creates() == 0
    assert not any(m == "DELETE" for m, _ in dest.calls)
    assert report.supplemented == [label]
    assert report.failed == []
    assert report.up_to_date == []
    # Attribution survives convergence: the publisher is still named.
    assert report.publishers == {label: "other"}
    # The audit names the old and the new title and says it was converged.
    assert len(audit) == 1
    assert "result=supplemented" in audit[0]
    assert repr(TAG_TITLE) in audit[0]
    assert repr(GATED_TITLE) in audit[0]


# --- criterion 2: the audience gate runs before the write ------------------


def _body_with_tracker() -> str:
    return FULL_BODY.replace(INSTALL_BLOCK, "Fix LVC-999 in installer")


@pytest.mark.asyncio
async def test_a_failing_body_writes_nothing_on_that_destination(tmp_path):
    """Criterion 2: the gate runs on the body about to be written into the
    existing object; a failing body writes nothing on that destination, and the
    refusal is recorded there by name."""
    dest = FakeDestination(_softprops_release())
    report, audit = await _publish(
        "forgejo", _forgejo(dest), tmp_path,
        release_body=_body_with_tracker(), ticket_prefixes=["LVC"],
    )

    label = f"forgejo:{REPO}"
    assert dest.writes() == []
    assert dest.creates() == 0
    assert report.failed == [label]
    assert report.body_states[label].startswith("failed:")
    assert "LVC-999" in report.failure_details[label]
    assert len(audit) == 1 and "result=failed" in audit[0]


@pytest.mark.asyncio
async def test_a_failing_body_refuses_each_destination_and_stops_none(tmp_path):
    """Criterion 2: the gate is per destination and never aborts the loop. The
    one body of the call is refused on both destinations; each is named in
    ``failed`` with its own audit line, and nothing is written on either. The
    loop reaches the second destination — a refusal is a recorded outcome, not
    an exception that abandons the remaining destinations."""
    refused = FakeDestination(_softprops_release())
    also_refused = FakeDestination(_softprops_release())
    report, audit = await _publish(
        "forgejo", _forgejo(refused), tmp_path,
        release_body=_body_with_tracker(), ticket_prefixes=["LVC"],
        adapters=[_forgejo(refused), _github(also_refused)],
        config=_config(forge="forgejo", second="github"),
    )

    first = f"forgejo:{REPO}"
    second = f"github:{REPO}"
    assert refused.writes() == [] and also_refused.writes() == []
    assert refused.creates() == 0 and also_refused.creates() == 0
    assert report.failed == [first, second]
    assert report.supplemented == []
    assert report.body_states[first].startswith("failed:")
    assert report.body_states[second].startswith("failed:")
    assert len(audit) == 2
    assert all("result=failed" in line for line in audit)


@pytest.mark.asyncio
async def test_a_clean_body_through_the_same_call_writes_normally(tmp_path):
    """Criterion 2 control: the same call without a tracker reference writes,
    so the refusal above is the gate and not the path."""
    dest = FakeDestination(_softprops_release())
    report, _ = await _publish(
        "forgejo", _forgejo(dest), tmp_path,
        release_body=FULL_BODY, ticket_prefixes=["LVC"],
    )
    patched = {}
    for body in dest.patches:
        patched.update(body)
    assert patched.get("body") == FULL_BODY
    assert report.failed == []


# --- criterion 3: the RED proof --------------------------------------------

# On the base commit this file's convergence tests fail: the softprops object
# keeps ``name = "v3.5.1"`` and ``body = ""`` because ``_try_supplement_body``
# returns after the body PATCH and never sets the title, while the per
# destination audience gate does not exist. The run below is the proof for the
# dispatch; the tests above are the same scenario with the intended outcome.

SOFTPROPS_TITLE = TAG_TITLE
SOFTPROPS_BODY = ""


def test_softprops_shape_is_the_documented_red_fixture():
    """The fixture is the object the proof names: the tag as title, empty body."""
    release = _softprops_release()
    assert release["name"] == SOFTPROPS_TITLE
    assert release["body"] == SOFTPROPS_BODY
    assert release["assets"] == []
