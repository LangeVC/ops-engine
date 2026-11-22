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
from ops_engine.config_loader import OpsEngineConfig, OpsYamlError, load_ops_yaml
from ops_engine.modules.release import compose_release_body
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


def _config(
    forge: str,
    *,
    second: str | None = None,
    assets=None,
    tracker_prefixes=None,
    install_block=None,
    manifest=None,
    forgejo_assets=None,
    free_space_url=None,
    forge_hosts=None,
) -> OpsEngineConfig:
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
    if manifest is not None:
        release["manifest"] = manifest
    if forgejo_assets is not None:
        release["forgejo_assets"] = forgejo_assets
    if free_space_url is not None:
        release["forgejo_free_space_url"] = free_space_url
    repository = {"destinations": destinations, "release": release}
    if tracker_prefixes is not None:
        repository["tracker_prefixes"] = tracker_prefixes
    if install_block is not None:
        repository["release_body_install_block"] = install_block
    if forge_hosts is not None:
        repository["forge_hosts"] = forge_hosts
    return OpsEngineConfig.load(
        {
            "orgs": {
                "langevc": {
                    "repositories": {"ops-engine": repository}
                }
            }
        }
    )


class FakeDestination:
    """Serve one release by tag and record every write (see REA-014 tests)."""

    def __init__(
        self, *, forge: str, hidden=(), tamper=None, create_status=409, preloaded=None
    ):
        self.forge = forge
        self.release: dict | None = None
        self.hidden = set(hidden)
        self.tamper = tamper or {}
        self.create_status = create_status
        self.calls: list[tuple[str, str]] = []
        self.uploaded: list[dict] = []
        self.read_assets: list[list[dict]] = []
        self.asset_bytes: dict[str, bytes] = {}
        self._next_id = 900
        if preloaded is not None:
            self.release = {
                "id": RELEASE_ID,
                "tag_name": TAG,
                "name": f"Ops Engine {TAG}",
                "body": BODY,
                "draft": False,
                "prerelease": False,
                "assets": [self._asset(name, data) for name, data in preloaded.items()],
            }

    def _asset(self, name: str, data: bytes) -> dict:
        self._next_id += 1
        self.asset_bytes[name] = data
        return {
            "id": self._next_id,
            "name": name,
            "size": len(data),
            "url": (
                f"https://{self.forge}.invalid/repos/{REPO}"
                f"/releases/assets/{self._next_id}"
            ),
            "digest": _sha256(data) if self.forge == "github" else None,
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if request.method == "GET" and "/releases/assets/" in path:
            name = next(
                (
                    asset["name"]
                    for asset in (self.release or {}).get("assets") or []
                    if asset.get("url") == str(request.url)
                ),
                None,
            )
            if name is None:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, content=self.asset_bytes[name])
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
            # Build the asset the way the forge answers: a served ``url`` and
            # the bytes behind it, so a pull can re-read what was attached.
            asset = self._asset(name, data)
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


async def _run(
    destinations,
    tmp_path,
    *,
    config,
    repo_dir=None,
    assets_body=BODY,
    free_space_reader=None,
):
    _artifacts(tmp_path)
    audit: list[str] = []
    extra = {} if free_space_reader is None else {"free_space_reader": free_space_reader}
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
        **extra,
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


def _config_file(
    tmp_path,
    forge: str,
    *,
    assets=None,
    tracker_prefixes=None,
    install_block=None,
    second=None,
    manifest=None,
    forgejo_assets=None,
    free_space_url=None,
    forge_hosts=None,
) -> "object":
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
    ]
    if second is not None:
        lines += [
            f"          - forge: {second}",
            f"            repo: {REPO}",
            "            role: release",
            "            credential_variable: SECOND_TOKEN",
        ]
    lines += [
        "        release:",
        "          enabled: true",
    ]
    if assets:
        lines.append("          assets:")
        for asset in assets:
            lines.append(f"            - type: {asset['type']}")
            lines.append(f"              name: '{asset['name']}'")
    if manifest:
        lines.append("          manifest:")
        if "deadline_minutes" in manifest:
            lines.append(
                f"            deadline_minutes: {manifest['deadline_minutes']}"
            )
        lines.append("            assets:")
        for piece in manifest.get("assets") or []:
            lines.append(f"              - type: {piece['type']}")
            lines.append(f"                name: '{piece['name']}'")
            lines.append(f"                producer: {piece['producer']}")
            lines.append("                destinations:")
            for label in piece["destinations"]:
                lines.append(f"                  - '{label}'")
    if forgejo_assets is not None:
        lines.append(f"          forgejo_assets: {forgejo_assets}")
    if free_space_url is not None:
        lines.append(f"          forgejo_free_space_url: '{free_space_url}'")
    if tracker_prefixes:
        lines.append("        tracker_prefixes:")
        for prefix in tracker_prefixes:
            lines.append(f"          - {prefix}")
    if install_block:
        lines.append(f"        release_body_install_block: '{install_block}'")
    if forge_hosts:
        lines.append("        forge_hosts:")
        for host in forge_hosts:
            lines.append(f"          - {host}")
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


def _argv(tmp_path, config_path, *, body_file=True) -> list[str]:
    argv = [
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
    ]
    if body_file:
        argv += ["--body-file", str(_body_file(tmp_path))]
    return argv


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


# --- REA-018: the declared body and the gate on the CLI path ----------------

TRACKER_IN_BLOCK = "Fix LVC-999 in the installer"


def test_the_cli_arms_the_gate_from_the_declared_prefixes(tmp_path, monkeypatch, capsys):
    """REA-018 C3 on the shipped console script: with no ``--ticket-prefix``,
    the repository's declared ``tracker_prefixes`` arm the gate, and the gate
    judges the body the engine composed (the install block, not a body file)."""
    monkeypatch.setenv("OPS_TOKEN", "rw-token")
    dest = FakeDestination(forge="forgejo")
    _artifacts(tmp_path)

    code = main(
        _argv(
            tmp_path,
            _config_file(
                tmp_path,
                "forgejo",
                assets=DECLARED,
                tracker_prefixes=["LVC"],
                install_block=TRACKER_IN_BLOCK,
            ),
            body_file=False,
        ),
        adapters=[_make(dest)],
    )
    payload = json.loads(capsys.readouterr().out)

    label = f"forgejo:{REPO}"
    assert code != 0
    assert payload["ok"] is False
    entry = payload["destinations"][0]
    assert entry["destination"] == label and entry["outcome"] == "failed"
    assert "LVC-999" in entry["failure"]
    assert dest.writes() == [], "the gate must refuse before any destination write"


@pytest.mark.asyncio
async def test_an_explicit_prefix_wins_and_the_declared_block_still_composes(tmp_path):
    """An explicit ``--ticket-prefix`` overrides the declared vocabulary; the
    declared install block still reaches both destinations without a body
    file (REA-018 C2 on the CLI path)."""
    first = FakeDestination(forge="forgejo")
    second = FakeDestination(forge="github")

    report = await run_attach(
        _config(
            "forgejo",
            second="github",
            tracker_prefixes=["LVC"],
            install_block=TRACKER_IN_BLOCK,
        ),
        REPO,
        TAG,
        tmp_path / "dist",
        adapters=[_make(first), _make(second)],
        repo_dir=tmp_path,
        name_template=TEMPLATE,
        notes=NOTES,
        ticket_prefixes=["REL"],
    )

    assert report.failed == []
    for dest in (first, second):
        assert f"### Install\n{TRACKER_IN_BLOCK}" in dest.release["body"]
        assert TRACKER_IN_BLOCK not in NOTES


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


# --- RB-002: the CLI composes the body the layover composes -----------------

# RB-002b: the link comes from the declared forge host, never the API base URL.
# On the runner ``FORGEJO_BASE_URL`` is the internal container address; the
# layover's link uses the organisation's public forge host, which is declared
# as ``forge_hosts`` in ``.ops.yaml``.
FORGE_HOST = "git.langevc.com"
FORGEJO_BASE_URL = f"https://{FORGE_HOST}"
RUNNER_BASE_URL = "http://forgejo:3000"
DERIVED_CHANGELOG_URL = f"https://{FORGE_HOST}/{REPO}/src/tag/{TAG}/CHANGELOG.md"
INTERNAL_CHANGELOG_URL = (
    f"{RUNNER_BASE_URL}/{REPO}/src/tag/{TAG}/CHANGELOG.md"
)
INSTALL_BLOCK = "pip install ops-engine"


def _composed(*, url: str = "", install_block: str = "") -> str:
    return compose_release_body(
        REPO,
        TAG,
        NOTES,
        name_template=TEMPLATE,
        install_block=install_block,
        changelog_url=url,
    )


def test_the_cli_derives_the_changelog_link_from_the_declared_forge_host(
    tmp_path, monkeypatch, capsys
):
    """RB-002b criterion 1+2: with ``forge_hosts`` declared and no flag, the
    command composes the layover's body — the link built from the declared
    public host, byte for byte. ``FORGEJO_BASE_URL`` is the runner's internal
    container address here; it must NOT reach the link.

    Against 56dfad4 the same run embeds ``http://forgejo:3000`` (the RUNNER value
    is what RB-002 derived from), so the body differs from the layover's; this
    asserts the intended end state, so that run is the RED proof.
    """
    monkeypatch.setenv("OPS_TOKEN", "rw-token")
    monkeypatch.setenv("FORGEJO_BASE_URL", RUNNER_BASE_URL)
    dest = FakeDestination(forge="forgejo")
    _artifacts(tmp_path)

    code = main(
        _argv(
            tmp_path,
            _config_file(
                tmp_path, "forgejo", assets=DECLARED, forge_hosts=[FORGE_HOST]
            ),
            body_file=False,
        ),
        adapters=[_make(dest)],
    )

    assert code == 0, capsys.readouterr().out
    assert dest.release["body"] == _composed(url=DERIVED_CHANGELOG_URL)
    assert RUNNER_BASE_URL not in dest.release["body"]


def test_an_explicit_changelog_url_overrides_the_derivation(
    tmp_path, monkeypatch, capsys
):
    """RB-002b criterion 1: ``--changelog-url`` wins over the derived link, so
    a host the derivation would not name is still composed as declared."""
    monkeypatch.setenv("OPS_TOKEN", "rw-token")
    monkeypatch.setenv("FORGEJO_BASE_URL", RUNNER_BASE_URL)
    override = "https://elsewhere.invalid/notes.md"
    dest = FakeDestination(forge="forgejo")
    _artifacts(tmp_path)

    argv = _argv(
        tmp_path,
        _config_file(tmp_path, "forgejo", assets=DECLARED, forge_hosts=[FORGE_HOST]),
        body_file=False,
    )
    argv += ["--changelog-url", override]
    code = main(argv, adapters=[_make(dest)])

    assert code == 0, capsys.readouterr().out
    assert dest.release["body"] == _composed(url=override)
    assert DERIVED_CHANGELOG_URL not in dest.release["body"]


def test_a_body_file_wins_over_the_derivation(tmp_path, monkeypatch, capsys):
    """RB-002b criterion 1 precedence: a supplied ``--body-file`` is published
    verbatim, so neither ``forge_hosts`` nor the derived link reaches the
    release."""
    monkeypatch.setenv("OPS_TOKEN", "rw-token")
    monkeypatch.setenv("FORGEJO_BASE_URL", RUNNER_BASE_URL)
    dest = FakeDestination(forge="forgejo")
    _artifacts(tmp_path)

    code = main(
        _argv(
            tmp_path,
            _config_file(tmp_path, "forgejo", assets=DECLARED, forge_hosts=[FORGE_HOST]),
            body_file=True,
        ),
        adapters=[_make(dest)],
    )

    assert code == 0, capsys.readouterr().out
    assert dest.release["body"] == BODY
    assert DERIVED_CHANGELOG_URL not in dest.release["body"]


def test_without_a_forge_host_no_link_is_invented(tmp_path, monkeypatch, capsys):
    """RB-002b criterion 1+3: with no ``forge_hosts`` and no flag, no link is
    invented even when ``FORGEJO_BASE_URL`` is set — the behaviour is exactly
    3.7.1. The composer still writes the declared parts, omits the link, and
    ``FORGEJO_BASE_URL`` never becomes a link."""
    monkeypatch.setenv("OPS_TOKEN", "rw-token")
    monkeypatch.setenv("FORGEJO_BASE_URL", RUNNER_BASE_URL)
    dest = FakeDestination(forge="forgejo")
    _artifacts(tmp_path)

    code = main(
        _argv(
            tmp_path,
            _config_file(
                tmp_path, "forgejo", assets=DECLARED, install_block=INSTALL_BLOCK
            ),
            body_file=False,
        ),
        adapters=[_make(dest)],
    )

    assert code == 0, capsys.readouterr().out
    assert dest.release["body"] == _composed(url="", install_block=INSTALL_BLOCK)
    assert "CHANGELOG.md" not in dest.release["body"]
    assert RUNNER_BASE_URL not in dest.release["body"]
    assert "### Install" in dest.release["body"]


# --- RB-002b: the loader reads forge_hosts, refusing a malformed value -------


def test_the_loader_reads_forge_hosts_from_ops_yaml(tmp_path):
    """RB-002b criterion 1: ``load_ops_yaml`` reads the declared ``forge_hosts``
    so the CLI can derive the link from ``forge_hosts[0]``."""
    (tmp_path / ".ops.yaml").write_text(
        "forge_hosts: [git.langevc.com]\n", encoding="utf-8"
    )

    layer3 = load_ops_yaml(tmp_path)

    assert layer3 is not None
    assert layer3.forge_hosts == ["git.langevc.com"]


def test_a_dotless_forge_host_is_refused_naming_key_and_file(tmp_path):
    """RB-002b criterion 1: a malformed ``forge_hosts`` value is refused naming
    the key (``forge_hosts``) and the file (``.ops.yaml``)."""
    (tmp_path / ".ops.yaml").write_text(
        "forge_hosts: [forgejo]\n", encoding="utf-8"
    )

    with pytest.raises(OpsYamlError) as exc_info:
        load_ops_yaml(tmp_path)

    message = str(exc_info.value)
    assert "forge_hosts" in message
    assert ".ops.yaml" in message
    assert str(tmp_path / ".ops.yaml") in message


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


def test_an_undeclared_org_is_a_named_usage_error_not_a_traceback(
    tmp_path, monkeypatch, capsys
):
    """Exit 2: deriving the CHANGELOG link reads the config before the run
    does; a config that does not declare the requested org is still reported
    as the documented named usage error, never a traceback."""
    monkeypatch.setenv("OPS_TOKEN", "rw-token")
    monkeypatch.setenv("FORGEJO_BASE_URL", FORGEJO_BASE_URL)
    _artifacts(tmp_path)
    argv = _argv(tmp_path, _config_file(tmp_path, "forgejo"), body_file=False)
    argv[argv.index("--repo") + 1] = "other/thing"

    code = main(argv, adapters=[])
    payload = json.loads(capsys.readouterr().out)

    assert code == 2
    assert payload["ok"] is False and payload["exit_code"] == 2
    assert payload["error"].startswith("ConfigSectionError:")
    assert "no org named 'other'" in payload["error"]


# --- REA-017: the pull, the free space and the pending pieces in the report -


GITHUB_BINARY = "ops-engine-tool-3.5.1-linux-amd64.tar.gz"
GITHUB_BYTES = b"github built bytes\n"
FREE_SPACE_URL = "https://forge.invalid/space.json"
FREE_BYTES = 987654321
FREE_SPACE_RESPONSE = json.dumps({"free_bytes": FREE_BYTES}).encode()

PULL_MANIFEST = {
    "assets": [
        {
            "type": "github_release_tarball",
            "name": "ops-engine-tool-<version>-linux-amd64.tar.gz",
            "producer": "github",
            "destinations": [f"forgejo:{REPO}"],
        }
    ]
}

# A second manifest: the github-produced piece only GitHub must hold, so this
# run never reads it and it stays pending in the report.
PENDING_MANIFEST = {
    "assets": [
        *PULL_MANIFEST["assets"],
        {
            "type": "github_release_zip",
            "name": "ops-engine-tool-<version>-windows-amd64.zip",
            "producer": "github",
            "destinations": [f"github:{REPO}"],
        },
    ]
}
PENDING_BINARY = "ops-engine-tool-3.5.1-windows-amd64.zip"


class FreeSpaceSource:
    """Answer the declared free-space URL; record every request it sees.

    Stands in for the host endpoint the shipped reader reads. The shipped
    reader builds its own ``httpx`` client, so the listener is patched into the
    module's client factory; the real reader still parses the answer.
    """

    def __init__(self, body=FREE_SPACE_RESPONSE, status=200):
        self.body = body
        self.status = status
        self.reads: list[tuple[str, str | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.reads.append(
            (str(request.url), request.headers.get("authorization"))
        )
        return httpx.Response(self.status, content=self.body)


def _patch_default_client(monkeypatch, source: FreeSpaceSource, fakes) -> None:
    """Give the shipped reader a mock transport without touching the network.

    The reader builds its own ``httpx.AsyncClient``: every default client the
    asset-pull module opens carries this transport, which answers the declared
    URL from ``source`` and serves each forge's asset bytes from its fake.
    """
    from ops_engine.modules import asset_pull

    real_client = httpx.AsyncClient

    def build_transport(request: httpx.Request) -> httpx.Response:
        if str(request.url) == FREE_SPACE_URL:
            return source.handler(request)
        for fake in fakes:
            if f"{fake.forge}.invalid" in str(request.url):
                return fake.handler(request)
        return httpx.Response(500, json={"message": "unexpected read"})

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(build_transport)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(asset_pull.httpx, "AsyncClient", client_factory)


def _github_pair(*, github_preloaded):
    """A Forgejo release beside the GitHub release that holds the binary."""
    forgejo = FakeDestination(forge="forgejo")
    github = FakeDestination(
        forge="github",
        preloaded={**{name: CONTENT[name] for name in FILES}, **github_preloaded},
    )
    return forgejo, github


def test_the_cli_report_carries_the_pulled_free_space_and_pending(
    tmp_path, monkeypatch, capsys
):
    """REA-017 on the CLI path: a ``pull`` run whose JSON report carries, per
    destination, the pulled file names, the free-space reading (an integer
    byte count here) and the still-pending github-produced piece.

    The pull means: the GitHub-built tarball lands on the Forgejo release with
    the GitHub digest, and the free space is read from the declared source.
    """
    monkeypatch.setenv("OPS_TOKEN", "rw-token")
    monkeypatch.setenv("SECOND_TOKEN", "rw-token")
    forgejo, github = _github_pair(github_preloaded={GITHUB_BINARY: GITHUB_BYTES})
    _artifacts(tmp_path)
    adapters = [_make(forgejo), _make(github)]
    source = FreeSpaceSource()
    _patch_default_client(monkeypatch, source, [forgejo, github])

    code = main(
        _argv(
            tmp_path,
            _config_file(
                tmp_path,
                "forgejo",
                second="github",
                assets=DECLARED,
                manifest=PENDING_MANIFEST,
                forgejo_assets="pull",
                free_space_url=FREE_SPACE_URL,
            ),
        ),
        adapters=adapters,
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0, payload
    entries = {e["destination"]: e for e in payload["destinations"]}
    forgejo_entry = entries[f"forgejo:{REPO}"]
    github_entry = entries[f"github:{REPO}"]
    assert forgejo_entry["pulled"] == [GITHUB_BINARY]
    assert forgejo_entry["free_space"] == FREE_BYTES
    assert source.reads == [(FREE_SPACE_URL, None)], (
        "the declared source is read once with no credential"
    )
    pending = github_entry["pending"]
    assert len(pending) == 1 and pending[0]["name"] == PENDING_BINARY
    assert pending[0]["producer"] == "github" and pending[0]["since"]
    assert GITHUB_BINARY in [a["name"] for a in forgejo.uploaded]


def test_the_cli_report_names_an_undeclared_free_space_source(
    tmp_path, monkeypatch, capsys
):
    """REA-017: with no source declared the reading is an explicit
    ``unavailable: ...`` string, never null, and the release still succeeds."""
    monkeypatch.setenv("OPS_TOKEN", "rw-token")
    dest = FakeDestination(forge="forgejo")
    _artifacts(tmp_path)

    code = main(
        _argv(
            tmp_path,
            _config_file(
                tmp_path, "forgejo", assets=DECLARED,
                forgejo_assets="pull", free_space_url=None,
            ),
        ),
        adapters=[_make(dest)],
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    entry = payload["destinations"][0]
    assert entry["pulled"] == []
    assert entry["pending"] == []
    assert entry["free_space"].startswith("unavailable:")
    assert "no source declared" in entry["free_space"]


# --- The Forgejo host reaches the engine on the shipped command -------------


def test_the_forgejo_base_url_from_the_environment_reaches_the_engine(
    tmp_path, monkeypatch, capsys
):
    """The release workflows export ``FORGEJO_BASE_URL``; the command passes it
    to the engine as ``base_url``. Without it a Forgejo destination built its
    adapter with an empty address and every request failed with "Request URL is
    missing an 'http://' or 'https://' protocol"."""
    from ops_engine.modules.release import PublicationReport, ReleaseHandler

    seen = {}

    async def capture(config, repo, tag_name, notes, artifact_dir, **kwargs):
        seen.update(kwargs)
        return PublicationReport()

    monkeypatch.setattr(ReleaseHandler, "publish_release", staticmethod(capture))
    monkeypatch.setenv("OPS_TOKEN", "rw-token")
    monkeypatch.setenv("FORGEJO_BASE_URL", "https://forge.example")
    _artifacts(tmp_path)

    main(_argv(tmp_path, _config_file(tmp_path, "forgejo", assets=DECLARED)))
    capsys.readouterr()

    assert seen.get("base_url") == "https://forge.example"


def test_the_base_url_is_empty_when_the_environment_names_none(
    tmp_path, monkeypatch, capsys
):
    from ops_engine.modules.release import PublicationReport, ReleaseHandler

    seen = {}

    async def capture(config, repo, tag_name, notes, artifact_dir, **kwargs):
        seen.update(kwargs)
        return PublicationReport()

    monkeypatch.setattr(ReleaseHandler, "publish_release", staticmethod(capture))
    monkeypatch.setenv("OPS_TOKEN", "rw-token")
    monkeypatch.delenv("FORGEJO_BASE_URL", raising=False)
    _artifacts(tmp_path)

    main(_argv(tmp_path, _config_file(tmp_path, "forgejo", assets=DECLARED)))
    capsys.readouterr()

    assert seen.get("base_url") == ""


# --- RB-004: an undecidable body mismatch names its cause and the remedy ----


def _populate_different_body(dest: FakeDestination, body: str) -> None:
    """An existing release whose body is genuinely not this build's, with the
    title and assets that do match."""
    dest.release = {
        "id": RELEASE_ID,
        "tag_name": TAG,
        "name": f"Ops Engine {TAG}",
        "body": body,
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "id": 900,
                "name": name,
                "size": len(CONTENT[name]),
                "digest": _sha256(CONTENT[name]) if dest.forge == "github" else None,
            }
            for name in FILES
        ],
    }


def test_rb004_notes_differ_names_forge_hosts_and_changelog_url(
    tmp_path, monkeypatch, capsys
):
    """RB-004 criterion 1 (as amended by RB-002b): with no link available —
    no ``forge_hosts`` and no flag — the CLI could not compose a body, so a
    differing existing body is ``notes differ``; the failure detail must name
    the two inputs that make the layover's body: ``forge_hosts`` and
    ``--changelog-url``. ``FORGEJO_BASE_URL`` is no longer a remedy, so it is
    absent from the message even when set."""
    monkeypatch.setenv("OPS_TOKEN", "rw-token")
    monkeypatch.setenv("FORGEJO_BASE_URL", RUNNER_BASE_URL)
    dest = FakeDestination(forge="forgejo")
    _populate_different_body(dest, "A body this build cannot reproduce.\n")
    _artifacts(tmp_path)

    code = main(
        _argv(
            tmp_path, _config_file(tmp_path, "forgejo", assets=DECLARED), body_file=False
        ),
        adapters=[_make(dest)],
    )
    payload = json.loads(capsys.readouterr().out)

    assert code != 0
    entry = payload["destinations"][0]
    assert entry["outcome"] == "failed"
    assert "notes differ" in entry["failure"]
    assert "forge_hosts" in entry["failure"]
    assert "--changelog-url" in entry["failure"]
    assert "FORGEJO_BASE_URL" not in entry["failure"]
    assert dest.writes() == [], "a differing body is never overwritten (D1)"


def test_rb004_a_genuinely_different_body_still_refuses_with_a_url_available(
    tmp_path, monkeypatch, capsys
):
    """RB-004 criterion 2: with a link available (``forge_hosts`` declared) the
    CLI composes; a body that is genuinely not this build's is still refused
    and nothing is written. The remedy sentence is only for the undecidable
    case."""
    monkeypatch.setenv("OPS_TOKEN", "rw-token")
    monkeypatch.setenv("FORGEJO_BASE_URL", RUNNER_BASE_URL)
    dest = FakeDestination(forge="forgejo")
    _populate_different_body(dest, "An entirely different body.\n")
    _artifacts(tmp_path)

    code = main(
        _argv(
            tmp_path,
            _config_file(tmp_path, "forgejo", assets=DECLARED, forge_hosts=[FORGE_HOST]),
            body_file=False,
        ),
        adapters=[_make(dest)],
    )
    payload = json.loads(capsys.readouterr().out)

    assert code != 0
    entry = payload["destinations"][0]
    assert entry["outcome"] == "failed"
    assert "the CLI composed no body to compare" not in entry["failure"]
    assert dest.writes() == [], "a differing body is never overwritten (D1)"
