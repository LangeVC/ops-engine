"""REA-015 (attempt 4): the command runs without a Layer-2 config file.

A product repository that declares its release work in its own ``.ops.yaml``
must not ship or invent a Layer-2 org ``config.yml`` just to publish. The same
argv — minus ``--config`` — resolves the destinations, the release block and
``release.assets`` from the Layer-3 file in ``--repo-dir`` and does the whole
job: find or create, converge, attach and verify.

When ``--repo-dir`` carries no ``.ops.yaml``, nothing declares where to
publish: the command refuses with the usage exit code and names the file. The
same holds for a ``.ops.yaml`` that declares no destination.

``--config`` is unchanged: with it the command reads the same Layer-2 file it
always read (``tests/test_release_attach_cli.py`` is that contract and stays
untouched).

The fake destinations are ``httpx`` transports behind the real adapters, as in
``test_release_attach_cli.py``.
"""

import hashlib
import json
from pathlib import Path

import httpx

from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
from ops_engine.adapters.github_adapter import GithubAdapter
from ops_engine.release_attach import main

REPO = "langevc/ops-engine"
TAG = "v3.5.1"
TEMPLATE = "Ops Engine {tag_name}"
NOTES = "- A feature.\n\n- A fix.\n"
RELEASE_ID = 77

FILES = [
    "ops_engine-3.5.1-py3-none-any.whl",
    "ops_engine-3.5.1.tar.gz",
    "SHA256SUMS",
]
CONTENT = {name: f"built bytes of {name}\n".encode() for name in FILES}


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class FakeDestination:
    """Serve one release by tag and record every write (see REA-014 tests)."""

    def __init__(self, *, forge: str):
        self.forge = forge
        self.release: dict | None = None
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
            data = request.content
            asset = {"name": name, "size": len(data)}
            asset["digest"] = _sha256(data) if self.forge == "github" else None
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
        return [(m, p) for m, p in self.calls if m != "GET"]


def _forgejo(dest: FakeDestination) -> ForgejoAdapter:
    adapter = ForgejoAdapter(
        base_url="https://forge.invalid", token="t", webhook_secret="s"
    )
    adapter._client = httpx.AsyncClient(
        base_url="https://forge.invalid/api/v1",
        transport=httpx.MockTransport(dest.handler),
    )
    return adapter


def _github(dest: FakeDestination) -> GithubAdapter:
    adapter = GithubAdapter(token="t", webhook_secret="s")
    adapter._client = httpx.AsyncClient(
        base_url="https://api.github.invalid",
        transport=httpx.MockTransport(dest.handler),
    )
    return adapter


OPS_YAML = "\n".join(
    [
        "destinations:",
        "  - forge: forgejo",
        f"    repo: {REPO}",
        "    role: release",
        "    credential_variable: OPS_TOKEN",
        "  - forge: github",
        f"    repo: {REPO}",
        "    role: release",
        "    credential_variable: SECOND_TOKEN",
        "release:",
        "  enabled: true",
        "  assets:",
        "    - type: github_release_zip",
        "      name: 'ops_engine-<version>-py3-none-any.whl'",
        "    - type: github_release_tarball",
        "      name: 'ops_engine-<version>.tar.gz'",
        "    - type: github_release_tarball",
        "      name: 'SHA256SUMS'",
    ]
) + "\n"


def _artifacts(tmp_path) -> None:
    art = tmp_path / "dist"
    art.mkdir(exist_ok=True)
    for name, data in CONTENT.items():
        (art / name).write_bytes(data)


def _argv(tmp_path: Path) -> list[str]:
    notes = tmp_path / "notes.md"
    notes.write_text(NOTES, encoding="utf-8")
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
        str(notes),
    ]


def test_the_repository_ops_yaml_alone_drives_publish_converge_and_verify(
    tmp_path, monkeypatch, capsys
):
    """No ``--config``: the ``.ops.yaml`` in ``--repo-dir`` declares both
    destinations and ``release.assets``; the run publishes, converges (a second
    run finds the same objects) and verifies the declared assets."""
    monkeypatch.setenv("OPS_TOKEN", "rw-token")
    monkeypatch.setenv("SECOND_TOKEN", "rw-token-2")
    (tmp_path / ".ops.yaml").write_text(OPS_YAML, encoding="utf-8")
    _artifacts(tmp_path)
    forgejo = FakeDestination(forge="forgejo")
    github = FakeDestination(forge="github")
    adapters = [_forgejo(forgejo), _github(github)]

    code = main(_argv(tmp_path), adapters=adapters)
    payload = json.loads(capsys.readouterr().out)

    assert code == 0, payload
    assert payload["ok"] is True
    assert [entry["destination"] for entry in payload["destinations"]] == [
        f"forgejo:{REPO}",
        f"github:{REPO}",
    ]
    for entry in payload["destinations"]:
        assert entry["outcome"] in {"published", "converged", "verified"}
        assert sorted(entry["verified"]) == sorted(
            f"{name} {_sha256(CONTENT[name])}" for name in FILES
        )
    for dest in (forgejo, github):
        assert sorted(a["name"] for a in dest.release["assets"]) == sorted(FILES)

    before = {dest.forge: dict(dest.release) for dest in (forgejo, github)}

    # The same argv again: a release already present converges to the same
    # objects (the same inputs, before or after the layover, end equally).
    code_again = main(_argv(tmp_path), adapters=adapters)
    payload_again = json.loads(capsys.readouterr().out)

    assert code_again == 0, payload_again
    for dest in (forgejo, github):
        assert dest.release == before[dest.forge]


def test_a_missing_ops_yaml_is_a_named_usage_error(tmp_path, monkeypatch, capsys):
    """No ``--config`` and no ``.ops.yaml``: nothing declares where to publish.
    Exit 2, naming the file the run looked for."""
    monkeypatch.setenv("OPS_TOKEN", "rw-token")
    _artifacts(tmp_path)

    code = main(_argv(tmp_path), adapters=[])
    payload = json.loads(capsys.readouterr().out)

    assert code == 2
    assert payload["ok"] is False and payload["exit_code"] == 2
    assert ".ops.yaml" in payload["error"]
    assert payload["destinations"] == []


def test_an_ops_yaml_without_a_destination_is_a_named_usage_error(
    tmp_path, monkeypatch, capsys
):
    """The file exists but declares no destination: still nothing to publish.
    Exit 2, naming the file."""
    monkeypatch.setenv("OPS_TOKEN", "rw-token")
    (tmp_path / ".ops.yaml").write_text(
        "release:\n  enabled: true\n", encoding="utf-8"
    )
    _artifacts(tmp_path)

    code = main(_argv(tmp_path), adapters=[])
    payload = json.loads(capsys.readouterr().out)

    assert code == 2
    assert payload["ok"] is False and payload["exit_code"] == 2
    assert ".ops.yaml" in payload["error"]
    assert payload["destinations"] == []
