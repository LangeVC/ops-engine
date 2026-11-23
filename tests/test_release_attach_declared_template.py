"""OE-101 / 3.7.3: the attach command follows the repository's declared title.

Measured 2026-11-23 (elementeer-pro run 48583): ``ops_engine.release_attach``
built the request with ``name_template=args.name_template or
DEFAULT_NAME_TEMPLATE`` although the same file already reads the merged
Layer-2/Layer-3 configuration for ``forge_hosts`` and the changelog path. A run
without ``--name-template`` therefore retitled an existing release to the
derived default (``{repo_name} {tag_name}``) even when ``.ops.yaml`` declared
``release.name_template``.

These tests run the shipped console entry (``main``) without ``--name-template``
against a repository whose ``.ops.yaml`` declares the title, and pin the
precedence the task fixes:

  * an explicit ``--name-template`` wins over the declaration;
  * the declaration wins over the default;
  * with no declaration and no flag the behaviour is exactly 3.7.2 (default);
  * a declaration that cannot be read is a named usage error, never a silent
    default.

RED proof: against b1a4e0d the first test exits failing because the existing
release titled ``Declared Title v1.2.3`` is converged to ``ops-engine v1.2.3``
(the derived default); the test asserts the title is unchanged, so the run is
the RED proof.

The fake destination is an ``httpx`` transport behind the real Forgejo adapter,
the same shape ``test_release_attach_cli.py`` uses.
"""

import hashlib
import json

import httpx
import pytest

from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
from ops_engine.release_attach import main

REPO = "langevc/ops-engine"
TAG = "v1.2.3"
DECLARED_TEMPLATE = "Declared Title {tag_name}"
DECLARED_TITLE = "Declared Title v1.2.3"
EXPLICIT_TEMPLATE = "Explicit Title {tag_name}"
EXPLICIT_TITLE = "Explicit Title v1.2.3"
# ``DEFAULT_NAME_TEMPLATE`` renders this for the repo below: no product prefix.
DEFAULT_TITLE = "ops-engine v1.2.3"
NOTES = "- A feature.\n\n- A fix.\n"
RELEASE_ID = 77

FILES = [
    "ops_engine-1.2.3-py3-none-any.whl",
    "ops_engine-1.2.3.tar.gz",
    "SHA256SUMS",
]
CONTENT = {name: f"built bytes of {name}\n".encode() for name in FILES}


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class FakeDestination:
    """Hold one existing release by tag and record every write.

    The release is preloaded as the layover would have left it: the intended
    title, the CHANGELOG section as body and every built file already attached,
    so a run that follows the declaration writes nothing.
    """

    def __init__(self, *, title: str):
        self.release: dict | None = {
            "id": RELEASE_ID,
            "tag_name": TAG,
            "name": title,
            "body": NOTES,
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "id": 900 + index,
                    "name": name,
                    "size": len(data),
                    # Forgejo reports ``digest: null`` (REL-010: size fallback).
                    "digest": None,
                }
                for index, (name, data) in enumerate(CONTENT.items())
            ],
        }
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if request.method == "GET" and "/releases/tags/" in path:
            if self.release is None:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json=self.release)
        if request.method == "GET" and path.endswith("/releases"):
            return httpx.Response(
                200, json=[] if self.release is None else [self.release]
            )
        if request.method == "GET" and path.endswith(
            f"/releases/{RELEASE_ID}/assets"
        ):
            assets = list((self.release or {}).get("assets") or [])
            page = request.url.params.get("page", "1")
            return httpx.Response(200, json=assets if page == "1" else [])
        if request.method == "PATCH" and path.endswith(f"/releases/{RELEASE_ID}"):
            body = json.loads(request.content)
            self.release = {**(self.release or {}), **body}
            return httpx.Response(200, json=self.release)
        if request.method == "POST" and path.endswith(
            f"/releases/{RELEASE_ID}/assets"
        ):
            name = request.url.params.get("name")
            asset = {
                "name": name,
                "size": len(request.content),
                "digest": None,
            }
            self.release = {
                **(self.release or {}),
                "assets": [*((self.release or {}).get("assets") or []), asset],
            }
            return httpx.Response(201, json=asset)
        if request.method == "POST" and path.endswith("/releases"):
            body = json.loads(request.content)
            self.release = {
                "id": RELEASE_ID,
                "tag_name": TAG,
                "name": body.get("name"),
                "body": body.get("body") or "",
                "draft": body.get("draft", False),
                "prerelease": body.get("prerelease", False),
                "assets": [],
            }
            return httpx.Response(201, json=self.release)
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


def _ops_yaml(tmp_path, *, name_template: str | None) -> None:
    """The Layer-3 declaration: one destination, plus the title when declared."""
    lines = [
        "destinations:",
        "  - forge: forgejo",
        f"    repo: {REPO}",
        "    role: release",
        "    credential_variable: OPS_TOKEN",
        "release:",
        "  enabled: true",
    ]
    if name_template is not None:
        lines.append(f"  name_template: '{name_template}'")
    (tmp_path / ".ops.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _artifacts(tmp_path) -> None:
    art = tmp_path / "dist"
    art.mkdir(exist_ok=True)
    for name, data in CONTENT.items():
        (art / name).write_bytes(data)


def _argv(tmp_path, *, name_template: str | None = None) -> list[str]:
    """The measured shape: ``--notes-file`` and ``--body-file``, no config file.

    ``--body-file`` carries the same CHANGELOG section the release already
    holds, so the only thing that could rewrite the release is a title
    difference. ``--name-template`` is added only when a test passes it.
    """
    notes = tmp_path / "notes.md"
    notes.write_text(NOTES, encoding="utf-8")
    body = tmp_path / "body.md"
    body.write_text(NOTES, encoding="utf-8")
    argv = [
        "--repo",
        REPO,
        "--tag",
        TAG,
        "--artifacts",
        str(tmp_path / "dist"),
        "--repo-dir",
        str(tmp_path),
        "--notes-file",
        str(notes),
        "--body-file",
        str(body),
    ]
    if name_template is not None:
        argv += ["--name-template", name_template]
    return argv


def test_an_existing_declared_title_survives_an_attach_without_the_flag(
    tmp_path, monkeypatch, capsys
):
    """Criterion 1: the release titled ``Declared Title v1.2.3`` is unchanged
    when the ``.ops.yaml`` declares the template and no ``--name-template`` is
    passed.

    Against b1a4e0d the request falls back to the derived default, so the run
    converges the title to ``ops-engine v1.2.3``; the assertion below fails and
    that run is the RED proof.
    """
    monkeypatch.setenv("OPS_TOKEN", "rw-token")
    _ops_yaml(tmp_path, name_template=DECLARED_TEMPLATE)
    _artifacts(tmp_path)
    dest = FakeDestination(title=DECLARED_TITLE)

    code = main(_argv(tmp_path), adapters=[_adapter(dest)])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0, payload
    assert payload["ok"] is True
    assert dest.release["name"] == DECLARED_TITLE
    assert dest.writes() == [], "the declared title must be left untouched"


def test_an_explicit_flag_wins_over_the_declaration(tmp_path, monkeypatch, capsys):
    """Criterion 2: an explicit ``--name-template`` overrides the declaration.

    The release carries the explicit title; the declaration names another. If
    the declaration wrongly won, the engine would retitle the release to
    ``Declared Title v1.2.3``.
    """
    monkeypatch.setenv("OPS_TOKEN", "rw-token")
    _ops_yaml(tmp_path, name_template=DECLARED_TEMPLATE)
    _artifacts(tmp_path)
    dest = FakeDestination(title=EXPLICIT_TITLE)

    code = main(
        _argv(tmp_path, name_template=EXPLICIT_TEMPLATE),
        adapters=[_adapter(dest)],
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0, payload
    assert payload["ok"] is True
    assert dest.release["name"] == EXPLICIT_TITLE
    assert dest.writes() == [], "the flag wins: nothing is retitled"


def test_the_declaration_wins_over_the_default(
    tmp_path, monkeypatch, capsys
):
    """Criterion 2: the declaration wins over the default.

    The release still carries the derived default title while the ``.ops.yaml``
    declares another. Without ``--name-template`` the engine must converge the
    title to the declared one — proving the declaration, not the default, names
    the release.
    """
    monkeypatch.setenv("OPS_TOKEN", "rw-token")
    _ops_yaml(tmp_path, name_template=DECLARED_TEMPLATE)
    _artifacts(tmp_path)
    dest = FakeDestination(title=DEFAULT_TITLE)

    code = main(_argv(tmp_path), adapters=[_adapter(dest)])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0, payload
    assert payload["ok"] is True
    assert dest.release["name"] == DECLARED_TITLE
    assert any(
        method == "PATCH" and path.endswith(f"/releases/{RELEASE_ID}")
        for method, path in dest.writes()
    ), dest.writes()


def test_without_a_declaration_and_a_flag_the_default_is_used(
    tmp_path, monkeypatch, capsys
):
    """Criterion 2: with no declaration and no flag the behaviour is exactly
    3.7.2 — the derived default template names the release."""
    monkeypatch.setenv("OPS_TOKEN", "rw-token")
    _ops_yaml(tmp_path, name_template=None)
    _artifacts(tmp_path)
    dest = FakeDestination(title=DEFAULT_TITLE)

    code = main(_argv(tmp_path), adapters=[_adapter(dest)])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0, payload
    assert payload["ok"] is True
    assert dest.release["name"] == DEFAULT_TITLE
    assert dest.writes() == [], "the default title is already in place"


def test_an_unreadable_declaration_is_a_named_usage_error(
    tmp_path, monkeypatch, capsys
):
    """A malformed ``.ops.yaml`` is a usage error naming the file, not a silent
    default.

    The run takes a Layer-2 ``--config`` (so ``request_from_args`` does not read
    ``.ops.yaml`` to resolve destinations) and no ``--name-template``: the only
    reader of ``.ops.yaml`` is the title resolution, which must refuse by name
    instead of falling back to the default.
    """
    monkeypatch.setenv("OPS_TOKEN", "rw-token")
    (tmp_path / ".ops.yaml").write_text("release: [\n", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(
        "\n".join(
            [
                "orgs:",
                "  langevc:",
                "    repositories:",
                "      ops-engine:",
                "        destinations:",
                "          - forge: forgejo",
                f"            repo: {REPO}",
                "            role: release",
                "            credential_variable: OPS_TOKEN",
                "        release:",
                "          enabled: true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _artifacts(tmp_path)
    dest = FakeDestination(title=DECLARED_TITLE)

    code = main(
        ["--config", str(config), *_argv(tmp_path)], adapters=[_adapter(dest)]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 2, payload
    assert payload["ok"] is False and payload["exit_code"] == 2
    assert ".ops.yaml" in payload["error"]
    assert payload["destinations"] == []
    assert dest.writes() == [], "a bad declaration writes nothing"
