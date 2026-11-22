"""RB-001 (ops-engine 3.7.2): a layover-composed existing release is refused.

The layover composes the release body itself — the heading, the CHANGELOG
section, the optional ``### Install`` block and assets line, and the CHANGELOG
link — and creates the release. The attach CLI then runs with the CHANGELOG
section as its notes and no body file: it passes those raw notes to
``ReleaseHandler.publish_release``, which compares the existing body against
the raw notes and refuses it as ``notes differ``. Measured on ledger v0.9.0
(run 48364, Forgejo release 468, GitHub release 450056416).

This file pins the shape before any change. It builds an existing release
whose body IS the engine-composed body (composed with the engine's own
``compose_release_body``) and runs the shipped console command with
``--notes-file`` only, once per declared body shape:

  * link only              — the ledger shape: heading, notes, CHANGELOG link
  * link plus install block — heading, notes, ``### Install`` block, link
  * link plus assets line   — heading, notes, assets line, link

Intent after RB-002: every shape ends with the release body still equal to the
layover composition byte for byte and no destination failed. Against 90dc06d
the link-only shape is refused with ``notes differ from the notes of this
build`` (the RED proof); the two shapes that declare a body part are silently
accepted at the base because the engine composes them without the link and
the supplement rule then treats the body as equal, which is why this file
judges the body itself, not only the exit code.

The fake destination is an ``httpx`` transport behind the real Forgejo
adapter, the same shape ``test_release_attach_cli.py`` uses.
"""

import hashlib
import json

import httpx
import pytest

from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
from ops_engine.modules.release import compose_release_body
from ops_engine.release_attach import main

REPO = "langevc/ops-engine"
TAG = "v3.5.1"
TEMPLATE = "Ops Engine {tag_name}"
NOTES = "- A feature.\n\n- A fix.\n"
CHANGELOG_PATH = "CHANGELOG.md"
INSTALL_BLOCK = "pip install ops-engine"
ASSETS_LINE = (
    "[ops_engine-3.5.1-py3-none-any.whl](https://git.langevc.com/"
    "langevc/ops-engine/releases/download/v3.5.1/"
    "ops_engine-3.5.1-py3-none-any.whl)"
)
RELEASE_ID = 77

# The organisation's canonical forge host, as declared in ``.ops.yaml``. RB-002b
# derives the CHANGELOG link from it (``forge_hosts[0]``) rather than from
# ``FORGEJO_BASE_URL``, which on a runner is the internal container address.
FORGE_HOST = "git.langevc.com"
# The address the release workflows export as FORGEJO_BASE_URL
# (``github.server_url``). On the runner this is ``http://forgejo:3000``; here
# it matches the public host so a reader sees no unrelated difference. The
# derivation must never read it.
FORGEJO_BASE_URL = f"https://{FORGE_HOST}"
CHANGELOG_URL = f"https://{FORGE_HOST}/{REPO}/src/tag/{TAG}/{CHANGELOG_PATH}"

FILES = [
    "ops_engine-3.5.1-py3-none-any.whl",
    "ops_engine-3.5.1.tar.gz",
    "SHA256SUMS",
]
CONTENT = {name: f"built bytes of {name}\n".encode() for name in FILES}


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _composed_body(*, install_block: str = "", assets_line: str = "") -> str:
    """The body the layover would have created for this repository.

    Composed with the engine's own composer, so "byte for byte" is judged
    against the one implementation the layover and the attach path share.
    """
    return compose_release_body(
        REPO,
        TAG,
        NOTES,
        name_template=TEMPLATE,
        install_block=install_block,
        assets_line=assets_line,
        changelog_path=CHANGELOG_PATH,
        changelog_url=CHANGELOG_URL,
    )


class FakeDestination:
    """Serve one release by tag and record every write (the REA-014 fake)."""

    def __init__(self, *, body: str):
        self.release: dict | None = {
            "id": RELEASE_ID,
            "tag_name": TAG,
            "name": TEMPLATE.format(tag_name=TAG),
            "body": body,
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "id": 900 + index,
                    "name": name,
                    "size": len(data),
                    # Forgejo reports ``digest: null`` (REL-010 fallback: size).
                    "digest": None,
                }
                for index, (name, data) in enumerate(CONTENT.items())
            ],
        }
        self.calls: list[tuple[str, str]] = []
        self.patched: list[dict] = []
        self.created: list[dict] = []
        self.uploaded: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if request.method == "GET" and "/releases/tags/" in path:
            if self.release is None:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json=self.release)
        if request.method == "GET" and path.endswith("/releases"):
            return httpx.Response(200, json=[] if self.release is None else [self.release])
        if (
            request.method == "GET"
            and path.endswith(f"/releases/{RELEASE_ID}/assets")
        ):
            assets = list((self.release or {}).get("assets") or [])
            page = request.url.params.get("page", "1")
            return httpx.Response(200, json=assets if page == "1" else [])
        if request.method == "PATCH" and path.endswith(f"/releases/{RELEASE_ID}"):
            body = json.loads(request.content)
            self.patched.append(body)
            self.release = {**(self.release or {}), **body}
            return httpx.Response(200, json=self.release)
        if request.method == "POST" and path.endswith(
            f"/releases/{RELEASE_ID}/assets"
        ):
            self.uploaded.append(request.url.params.get("name"))
            return httpx.Response(201, json={"name": request.url.params.get("name")})
        if request.method == "POST" and path.endswith("/releases"):
            self.created.append(json.loads(request.content))
            # Forgejo answers 409 for an existing release row (REA-011).
            return httpx.Response(409, json={"message": "exists"})
        return httpx.Response(
            500, json={"message": f"unexpected {request.method} {path}"}
        )

    def writes(self) -> list[tuple[str, str]]:
        return [(method, path) for method, path in self.calls if method != "GET"]


def _adapter(dest: FakeDestination) -> ForgejoAdapter:
    adapter = ForgejoAdapter(
        base_url="https://forge.invalid", token="t", webhook_secret="s"
    )
    adapter._client = httpx.AsyncClient(
        base_url="https://forge.invalid/api/v1",
        transport=httpx.MockTransport(dest.handler),
    )
    return adapter


def _ops_yaml(
    tmp_path, *, install_block: str = "", assets_line: str = ""
) -> None:
    """The repository's Layer-3 declaration: destinations and body parts.

    ``forge_hosts`` is declared because RB-002b derives the CHANGELOG link
    from it, not from ``FORGEJO_BASE_URL``; without it the CLI composes no
    link and the layover body (which carries one) could not be reproduced.
    """
    lines = [
        "forge_hosts:",
        f"  - {FORGE_HOST}",
        "destinations:",
        "  - forge: forgejo",
        f"    repo: {REPO}",
        "    role: release",
        "    visibility: public",
        "    credential_variable: OPS_TOKEN",
        "release:",
        "  enabled: true",
    ]
    if install_block:
        lines.append(f"release_body_install_block: '{install_block}'")
    if assets_line:
        lines.append(f"release_body_assets_line: '{assets_line}'")
    (tmp_path / ".ops.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _artifacts(tmp_path) -> None:
    art = tmp_path / "dist"
    art.mkdir(exist_ok=True)
    for name, data in CONTENT.items():
        (art / name).write_bytes(data)


def _notes_file(tmp_path) -> str:
    path = tmp_path / "notes.md"
    path.write_text(NOTES, encoding="utf-8")
    return str(path)


def _argv(tmp_path) -> list[str]:
    """``--notes-file`` only: no ``--body-file``, exactly the measured shape."""
    return [
        "--repo",
        REPO,
        "--tag",
        TAG,
        "--artifacts",
        str(tmp_path / "dist"),
        "--repo-dir",
        str(tmp_path),
        "--name-template",
        TEMPLATE,
        "--notes-file",
        _notes_file(tmp_path),
    ]


SHAPES = {
    "link_only": {"install_block": "", "assets_line": ""},
    "link_plus_install_block": {"install_block": INSTALL_BLOCK, "assets_line": ""},
    "link_plus_assets_line": {"install_block": "", "assets_line": ASSETS_LINE},
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_composed_existing_release_converges_without_overwrite(
    shape, tmp_path, monkeypatch, capsys
):
    """An existing release whose body is the layover composition is accepted
    byte for byte when attach runs with ``--notes-file`` only.

    Against 90dc06d the link-only shape exits non-zero with ``notes differ
    from the notes of this build``; the test asserts the intended end state
    (exit 0, body untouched), so that run is the RED proof.
    """
    parts = SHAPES[shape]
    body = _composed_body(**parts)
    dest = FakeDestination(body=body)
    _ops_yaml(tmp_path, **parts)
    _artifacts(tmp_path)
    monkeypatch.setenv("FORGEJO_BASE_URL", FORGEJO_BASE_URL)
    monkeypatch.setenv("OPS_TOKEN", "rw-token")

    code = main(_argv(tmp_path), adapters=[_adapter(dest)])
    output = capsys.readouterr().out
    payload = json.loads(output)

    # The failure text must reach the RED proof: a non-zero exit carries the
    # report with the destination's failure detail.
    assert code == 0 and payload["ok"] is True, output
    entry = payload["destinations"][0]
    assert entry["destination"] == f"forgejo:{REPO}"
    assert entry["failure"] is None
    assert entry["outcome"] != "failed"
    # The body is the layover composition, byte for byte, and was never
    # rewritten: D1 refuses to overwrite a differing body, and here nothing
    # differs.
    assert dest.release["body"] == body
    assert dest.patched == [], "the body must not be patched"
    assert dest.created == [], "the release must not be re-created"
    assert dest.uploaded == [], "the assets are already attached"
