"""REA-015: one entry point for repository workflows.

The console script ``ops-engine-release-attach`` takes a tag and an artifact
directory and, for every declared release destination, finds or creates the
release, converges it, attaches the assets and verifies them (REA-014).

Criterion 1: the same command works from a Forgejo runner and a GitHub runner
against a fake (the same argv; only the environment the runner supplies
differs).

Criterion 2: run before the layover (no release yet) and after it (a release
already present) with the same inputs, the final release objects are equal.

Criterion 3: the command prints a machine-readable report with one entry per
destination, and the exit code is 0 for done and non-zero for a named failure.

The two REA-014 review defects are fixed here as well:

* ``body_states`` keeps the pre-verification value after a verification
  failure (it must read ``failed: ...``).
* A superseded ``result=skipped`` audit line stays beside
  ``result=verify-failed`` for the same destination (only the failure line may
  remain).

The fake destination is an ``httpx`` transport behind the real adapters, as in
``test_release_asset_verify.py``.
"""

import hashlib
import json
from collections import Counter

import httpx
import pytest

from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
from ops_engine.adapters.github_adapter import GithubAdapter
from ops_engine.config_loader import OpsEngineConfig
from ops_engine.release_attach import main, render_report, run_attach

REPO = "langevc/ops-engine"
TAG = "v3.5.1"
TEMPLATE = "Ops Engine {tag_name}"
NOTES = "- A feature.\n\n- A fix.\n"
BODY = f"## Ops Engine {TAG}\n\n{NOTES}\n"
RELEASE_ID = 77

FILES = [
    "ops_engine-3.5.1-py3-none-any.whl",
    "ops_engine-3.5.1.tar.gz",
    "SHA256SUMS",
]
CONTENT = {name: f"built bytes of {name}\n".encode() for name in FILES}


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


DECLARED = [
    {"type": "github_release_zip", "name": "ops_engine-<version>-py3-none-any.whl"},
    {"type": "github_release_tarball", "name": "ops_engine-<version>.tar.gz"},
    {"type": "github_release_tarball", "name": "SHA256SUMS"},
]


def _config(forge: str, *, second: str | None = None, assets=None) -> OpsEngineConfig:
    destinations = [
        {
            "forge": forge,
            "repo": REPO,
            "role": "release",
            "credential_variable": "OPS_TOKEN",
        }
    ]
    if second is not None:
        destinations.append(
            {
                "forge": second,
                "repo": REPO,
                "role": "release",
                "credential_variable": "SECOND_TOKEN",
            }
        )
    release = {"enabled": True}
    if assets is not None:
        release["assets"] = assets
    return OpsEngineConfig.load(
        {
            "orgs": {
                "langevc": {
                    "repositories": {
                        "ops-engine": {
                            "destinations": destinations,
                            "release": release,
                        }
                    }
                }
            }
        }
    )


class FakeDestination:
    """Serve one release by tag and record every write (see REA-014 tests)."""

    def __init__(self, *, forge: str, hidden=(), tamper=None, create_status=409):
        self.forge = forge
        self.release: dict | None = None
        self.hidden = set(hidden)
        self.tamper = tamper or {}
        self.create_status = create_status
        self.calls: list[tuple[str, str]] = []
        self.uploaded: list[dict] = []
        self.read_assets: list[list[dict]] = []

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
            self.read_assets.append(assets)
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
            asset.update(self.tamper.get(name, {}))
            if name not in self.hidden:
                self.uploaded.append(asset)
                self.release = {
                    **(self.release or {}),
                    "assets": [*((self.release or {}).get("assets") or []), asset],
                }
            return httpx.Response(201, json=asset)
        if request.method == "POST" and path.endswith("/releases"):
            if self.release is not None:
                return httpx.Response(self.create_status, json={"message": "exists"})
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


def _make(dest: FakeDestination):
    return _forgejo(dest) if dest.forge == "forgejo" else _github(dest)


def _artifacts(tmp_path, extra: dict[str, bytes] | None = None) -> None:
    art = tmp_path / "dist"
    art.mkdir(exist_ok=True)
    for name, data in CONTENT.items():
        (art / name).write_bytes(data)
    for name, data in (extra or {}).items():
        (art / name).write_bytes(data)


async def _run(destinations, tmp_path, *, config, repo_dir=None, assets_body=BODY):
    _artifacts(tmp_path)
    audit: list[str] = []
    report = await run_attach(
        config,
        REPO,
        TAG,
        tmp_path / "dist",
        adapters=[_make(dest) for dest in destinations],
        repo_dir=tmp_path if repo_dir is None else repo_dir,
        name_template=TEMPLATE,
        notes=NOTES,
        release_body=assets_body,
        audit=audit,
    )
    return report, audit


def _prepopulate(dest: FakeDestination) -> None:
    """A release the layover left behind: same title/body/asset set."""
    assets = []
    for name in FILES:
        data = CONTENT[name]
        assets.append(
            {
                "name": name,
                "size": len(data),
                "digest": _sha256(data) if dest.forge == "github" else None,
            }
        )
    dest.release = {
        "id": RELEASE_ID,
        "tag_name": TAG,
        "name": f"Ops Engine {TAG}",
        "body": BODY,
        "draft": False,
        "prerelease": False,
        "assets": assets,
    }


def _objects(dest: FakeDestination) -> dict:
    """The final release object, order-independent for the asset list."""
    release = dest.release or {}
    return {
        "tag_name": release.get("tag_name"),
        "name": release.get("name"),
        "body": release.get("body"),
        "assets": sorted(
            (a["name"], a.get("size"), a.get("digest"))
            for a in release.get("assets") or []
        ),
    }


def _config_file(tmp_path, forge: str, *, assets=None) -> "object":
    path = tmp_path / "config.yml"
    lines = [
        "orgs:",
        "  langevc:",
        "    repositories:",
        "      ops-engine:",
        "        destinations:",
        f"          - forge: {forge}",
        f"            repo: {REPO}",
        "            role: release",
        "            credential_variable: OPS_TOKEN",
        "        release:",
        "          enabled: true",
    ]
    if assets:
        lines.append("          assets:")
        for asset in assets:
            lines.append(f"            - type: {asset['type']}")
            lines.append(f"              name: '{asset['name']}'")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _notes_file(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text(NOTES, encoding="utf-8")
    return path


def _body_file(tmp_path):
    path = tmp_path / "body.md"
    path.write_text(BODY, encoding="utf-8")
    return path


def _argv(tmp_path, config_path) -> list[str]:
    return [
        "--config",
        str(config_path),
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
        str(_notes_file(tmp_path)),
        "--body-file",
        str(_body_file(tmp_path)),
    ]


# --- Criterion 1: the same command from either runner -----------------------


@pytest.mark.parametrize("forge", ["forgejo", "github"])
def test_the_same_command_runs_from_either_runner(
    forge, tmp_path, monkeypatch, capsys
):
    """Criterion 1: identical argv; the runner only changes which credential
    variable is populated in the environment."""
    monkeypatch.setenv("OPS_TOKEN", "rw-token")
    dest = FakeDestination(forge=forge)
    _artifacts(tmp_path)

    code = main(
        _argv(tmp_path, _config_file(tmp_path, forge, assets=DECLARED)),
        adapters=[_make(dest)],
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert [entry["destination"] for entry in payload["destinations"]] == [
        f"{forge}:{REPO}"
    ]


# --- Criterion 2: before and after the layover ------------------------------


@pytest.mark.asyncio
async def test_before_and_after_the_layover_end_in_the_same_objects(tmp_path):
    """Criterion 2: no release yet vs a release already present from the
    layover, identical inputs — the final objects are equal."""
    before = FakeDestination(forge="forgejo")
    await _run([before], tmp_path, config=_config("forgejo", assets=DECLARED))

    after = FakeDestination(forge="forgejo")
    _prepopulate(after)
    await _run([after], tmp_path, config=_config("forgejo", assets=DECLARED))

    assert _objects(before) == _objects(after)
    assert _objects(before)["name"] == f"Ops Engine {TAG}"
    assert _objects(before)["body"] == BODY
    assert Counter(a[0] for a in _objects(before)["assets"]) == Counter(
        {name: 1 for name in FILES}
    )


# --- Criterion 3: machine-readable report and exit codes --------------------


@pytest.mark.asyncio
async def test_the_report_has_one_machine_readable_entry_per_destination(tmp_path):
    """Criterion 3: every destination appears once with its outcome and the
    verified name + SHA-256 list."""
    first = FakeDestination(forge="forgejo")
    second = FakeDestination(forge="github")
    report, _ = await _run(
        [first, second],
        tmp_path,
        config=_config("forgejo", second="github", assets=DECLARED),
    )

    payload = render_report(report)
    assert payload["schema"] == "ops-engine-release-attach-report/v1"
    assert payload["repo"] == REPO and payload["tag_name"] == TAG
    assert payload["ok"] is True and payload["exit_code"] == 0
    assert [e["destination"] for e in payload["destinations"]] == [
        f"forgejo:{REPO}",
        f"github:{REPO}",
    ]
    for entry in payload["destinations"]:
        assert entry["outcome"] in {"published", "verified"}
        assert entry["failure"] is None
        assert sorted(entry["verified"]) == sorted(
            f"{name} {_sha256(CONTENT[name])}" for name in FILES
        )
    json.dumps(payload)  # serialisable without a custom encoder


def test_the_command_prints_the_report_as_json(tmp_path, monkeypatch, capsys):
    """Criterion 3: stdout is JSON, not prose."""
    monkeypatch.setenv("OPS_TOKEN", "rw-token")
    dest = FakeDestination(forge="forgejo")
    _artifacts(tmp_path)

    code = main(
        _argv(tmp_path, _config_file(tmp_path, "forgejo", assets=DECLARED)),
        adapters=[_make(dest)],
    )
    out = capsys.readouterr().out

    assert code == 0
    payload = json.loads(out)
    assert payload["destinations"][0]["verified"]


def test_a_named_failure_exits_non_zero_and_names_file_and_destination(
    tmp_path, monkeypatch, capsys
):
    """Criterion 3: 0 done; non-zero a named failure. The JSON stays valid."""
    monkeypatch.setenv("OPS_TOKEN", "rw-token")
    dest = FakeDestination(forge="forgejo", hidden={"SHA256SUMS"})
    _artifacts(tmp_path)

    code = main(
        _argv(tmp_path, _config_file(tmp_path, "forgejo", assets=DECLARED)),
        adapters=[_make(dest)],
    )
    payload = json.loads(capsys.readouterr().out)

    label = f"forgejo:{REPO}"
    assert code != 0
    assert payload["ok"] is False and payload["exit_code"] == code
    entry = payload["destinations"][0]
    assert entry["destination"] == label
    assert entry["outcome"] == "failed"
    assert "SHA256SUMS" in entry["failure"]
    assert label in entry["failure"]


# --- The CLI wires the REA-014 verification ---------------------------------


@pytest.mark.asyncio
async def test_the_cli_passes_repo_dir_and_audit_so_verification_fires(tmp_path):
    """The run is driven through ``run_attach`` with the artifact directory and
    the repository root, so declared assets are read back and verified."""
    dest = FakeDestination(forge="forgejo")
    report, audit = await _run([dest], tmp_path, config=_config("forgejo", assets=DECLARED))

    assert report.verified[f"forgejo:{REPO}"]
    assert dest.read_assets, "the asset list was never read back"
    assert any("result=verified" in line for line in audit)


# --- REA-014 defects: body_states and the superseded audit line -------------


@pytest.mark.asyncio
async def test_body_states_records_the_verification_failure(tmp_path):
    """A destination that published but fails verification must not keep the
    pre-verification ``written`` state."""
    dest = FakeDestination(forge="forgejo", hidden={"SHA256SUMS"})
    report, _ = await _run([dest], tmp_path, config=_config("forgejo", assets=DECLARED))

    label = f"forgejo:{REPO}"
    assert report.failed == [label]
    assert report.body_states[label].startswith("failed: verification failed")
    assert "SHA256SUMS" in report.body_states[label]


@pytest.mark.asyncio
async def test_a_superseded_skipped_line_does_not_survive_a_verify_failure(tmp_path):
    """An already-equal release is skipped by the judgement; a declared file the
    build lacks then fails verification. Only the failure line may remain for
    that destination."""
    assets = [
        *DECLARED,
        {"type": "github_release_zip", "name": "not-built-<version>.zip"},
    ]
    dest = FakeDestination(forge="forgejo")
    await _run([dest], tmp_path, config=_config("forgejo", assets=DECLARED))
    _prepopulate(dest)

    report, audit = await _run([dest], tmp_path, config=_config("forgejo", assets=assets))

    label = f"forgejo:{REPO}"
    assert report.failed == [label]
    label_lines = [line for line in audit if f"dest={label} " in line]
    assert len(label_lines) == 1, label_lines
    assert "result=verify-failed" in label_lines[0]
    assert "not-built-3.5.1.zip" in label_lines[0]


# --- Named input errors are the usage exit code, never a traceback ----------


def test_a_missing_artifact_directory_is_a_named_usage_error(tmp_path, monkeypatch, capsys):
    """Exit 2: the run cannot start; the JSON says why and there is no
    destination entry."""
    monkeypatch.setenv("OPS_TOKEN", "rw-token")
    argv = _argv(tmp_path, _config_file(tmp_path, "forgejo", assets=DECLARED))
    argv[argv.index("--artifacts") + 1] = str(tmp_path / "absent")

    code = main(argv, adapters=[])
    payload = json.loads(capsys.readouterr().out)

    assert code == 2
    assert payload["ok"] is False and payload["exit_code"] == 2
    assert "artifact directory" in payload["error"]
    assert payload["destinations"] == []


def test_an_unreadable_config_is_a_named_usage_error(tmp_path, capsys):
    """Exit 2: a config file that does not parse is refused by name."""
    bad = tmp_path / "bad.yml"
    bad.write_text("orgs: [\n", encoding="utf-8")
    argv = _argv(tmp_path, bad)
    _artifacts(tmp_path)

    code = main(argv, adapters=[])
    payload = json.loads(capsys.readouterr().out)

    assert code == 2
    assert payload["error"].startswith("cannot load config")
