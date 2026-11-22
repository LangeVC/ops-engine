"""RB-003 (ops-engine 3.7.2): the layover body and the attach body agree.

Six real repositories, one test per shape. Each test drives the SAME body
through both paths and asserts it byte for byte:

* the layover path — ``ReleaseHandler.process_event`` → ``_release_tag``, the
  code the deployed release workflow runs, composing the body with
  ``compose_release_body`` from the ``.ops.yaml`` read at the tag;
* the attach path — ``ops_engine.release_attach.main`` with ``--notes-file``
  only (no ``--body-file``), so the CLI composes the body itself exactly as the
  measured production failure did (ledger v0.9.0, run 48364).

The six fixtures live in ``tests/fixtures/release_body_parity/``. Each is a
verbatim copy of that repository's default-branch ``.ops.yaml`` with the source
commit SHA in a header comment; nothing is fetched from a forge. The shapes are:

  * ``skillweave-sdk`` — link only, three declared assets, public both forges
  * ``skillweave``     — link only, three declared assets, 38 tracker prefixes
  * ``capacium``       — link only, a release manifest (six github pieces plus
                         three channels), private Forgejo / public GitHub
  * ``elementeer-bridge`` — link only, no ``release:`` block at all
  * ``elementeer``     — link only, one declared asset
  * ``elementeer-addon-voxel`` — link only, one declared asset, six prefixes

None of the six declares ``release_body_install_block`` or
``release_body_assets_line`` (grep: three comment-only mentions in the
elementeer files, zero actual keys). So every shape here is the "link only"
form; the paragraph record is RB-001's, where the install block and assets
line were declared synthetically.

Recorded finding for RB-003 criterion 2 — do the layover's identity.canonical_url
host and FORGEJO_BASE_URL name the same host in the deployed config? **No.**

  * The layover builds ``canonical_url = identity.canonical_url`` from the
    Forgejo instance it talks to: ``DEFAULT_FORGEJO_URL`` / ``FORGEJO_URL`` in
    lvc-ops ``src/config_parser.py`` is ``https://git.langevc.com`` and
    ``identity.canonical_url = f"{forgejo_url}/{repo}.git"``; the deployed
    Forgejo declares ``FORGEJO__server__ROOT_URL=https://git.langevc.com``
    (server/langevc-prod-01/compose/docker-compose.forgejo.yml). The declared
    canonical host is therefore ``git.langevc.com``.
  * The release workflows export ``FORGEJO_BASE_URL: ${{ github.server_url }}``
    (``.forgejo/workflows/forgejo-release.yml``). On the Forgejo runner
    ``github.server_url`` resolves to ``http://forgejo:3000`` — the runner's
    internal container address, which is not the organisation's forge
    (measured: ``forgejo-runner-config.yml`` ``url: http://forgejo:3000``;
    stated in this repository's ``.ops.yaml`` and ``CONTRACT.md``; the three
    elementeer ``.ops.yaml`` files carry ``forge_hosts: [git.langevc.com]``
    for exactly this reason).

They differ. Consequence: RB-002's derivation reproduces the layover URL byte
for byte only when ``FORGEJO_BASE_URL`` names the declared canonical host. The
parity tests therefore set ``FORGEJO_BASE_URL`` to ``https://git.langevc.com``
(the declared canonical host) and hand the CLI the same ``--name-template`` the
repository declares, which is how the release workflow invokes it. RB-002's
derivation itself is pinned separately in
``test_the_derivation_uses_the_declared_canonical_host``, which proves the URL
shape and shows the runner address would produce a different one.

RED proof (RB-003 criterion 3): against the RB-001 base (90dc06d) the attach
path has no ``changelog_url`` at all, so its effective body stays ``None`` and
the existing release is refused as ``notes differ from the notes of this
build``. Since no fixture declares an install block or assets line, the
criterion's named subset is empty; the failure is proven for all six on the
missing-CHANGELOG-link reason instead — the reason is recorded, not forced.

The fakes are real adapters over ``httpx.MockTransport``, the idiom
``test_release_attach_cli.py`` and ``test_release_attach_composed_body.py``
use.
"""

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
from ops_engine.adapters.github_adapter import GithubAdapter
from ops_engine.config_loader import OpsEngineConfig, ReleaseConfig, load_ops_yaml
from ops_engine.modules import release as release_module
from ops_engine.modules.release import (
    ReleaseContext,
    ReleaseHandler,
    TagIdentitySpec,
    _declared_release_asset_names,
)
from ops_engine.release_attach import _parser, main, request_from_args

FIXTURES = Path(__file__).parent / "fixtures" / "release_body_parity"

# The declared canonical forge host (see the module docstring). The layover
# renders this host into canonical_url; the parity command is handed it as
# FORGEJO_BASE_URL so the derivation reproduces the layover link byte for byte.
CANONICAL_HOST = "https://git.langevc.com"
# The value github.server_url actually has on the Forgejo runner, kept here so
# the criterion-2 test can show the two hosts differ.
RUNNER_BASE_URL = "http://forgejo:3000"

TAG = "v1.2.3"
NOTES = "- A feature.\n\n- A fix."
CHANGELOG_TEXT = f"# Changes\n\n## {TAG}\n\n{NOTES}\n"
RELEASE_ID = 500


def _now_iso() -> str:
    """The release clock the fakes stamp, set to the run's own time.

    Capacium's manifest names six github-produced pieces that GitHub Actions
    publishes downstream; a release created now keeps them pending inside the
    60-minute deadline, never red on a clock the fake invented.
    """
    return datetime.now(timezone.utc).isoformat()

# One entry per repository shape. ``sha`` is the source commit recorded in the
# fixture header; ``name_template`` is the release block's declared title form
# (None when the repository declares no release block or no template), which
# the attach command is given as --name-template.
SHAPES = {
    "skillweave-sdk": {
        "filename": "skillweave-sdk.ops.yaml",
        "repo": "skillweave/skillweave-sdk",
        "sha": "9aeddf6ede84bdc68869cf86e6f5c4318469295c",
        "name_template": "SkillWeave SDK {tag_name}",
    },
    "skillweave": {
        "filename": "skillweave.ops.yaml",
        "repo": "skillweave/skillweave",
        "sha": "3a3b4e9f5860134391d98f71796653d43dfb0608",
        "name_template": "SkillWeave {tag_name}",
    },
    "capacium": {
        "filename": "capacium.ops.yaml",
        "repo": "capacium/capacium",
        "sha": "1e54634b3eaf4c8275d08e10416b4fe2770495c1",
        "name_template": None,
    },
    "elementeer-bridge": {
        "filename": "elementeer-bridge.ops.yaml",
        "repo": "elementeer/elementeer-bridge",
        "sha": "73af4a07b1888f3c65c76ed5f0a7d39c2aa5c350",
        "name_template": None,
    },
    "elementeer": {
        "filename": "elementeer.ops.yaml",
        "repo": "elementeer/elementeer",
        "sha": "8b066051dd1d5d08519490539d48c2b1797460c6",
        "name_template": "Elementeer {tag_name}",
    },
    "elementeer-addon-voxel": {
        "filename": "elementeer-addon-voxel.ops.yaml",
        "repo": "elementeer/elementeer-addon-voxel",
        "sha": "d0a0d908ff287716e07808b7202dcf472f54cd1d",
        "name_template": "Elementeer Voxel Addon {tag_name}",
    },
}


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _shape_config(repo: str) -> OpsEngineConfig:
    """The minimal Layer-2 stand-in the attach path builds for a bare repo."""
    org, _, name = repo.partition("/")
    return OpsEngineConfig.load(
        {"orgs": {org: {"repositories": {name: {}}}}}
    )


class FakeForge:
    """Serve one repository's files and one release per forge, recording writes.

    The same object answers the layover's raw-file reads (``get_file_content``)
    and the release API, so one adapter drives a whole parity run.
    """

    def __init__(self, *, forge: str, changelog: str = "", ops_yaml: str = ""):
        self.forge = forge
        self.changelog = changelog
        self.ops_yaml = ops_yaml
        self.release: dict | None = None
        self.created: list[dict] = []
        self.patched: list[dict] = []
        self.uploaded: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self._next_id = 900

    def _asset(self, name: str, data: bytes) -> dict:
        self._next_id += 1
        return {
            "id": self._next_id,
            "name": name,
            "size": len(data),
            # Forgejo reports digest: null (REL-010 falls back to size);
            # GitHub reports sha256:<hex>.
            "digest": _sha256(data) if self.forge == "github" else None,
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))

        # Raw file reads (the layover's get_file_content) — Forgejo only.
        if request.method == "GET" and "/raw/" in path:
            if path.endswith("/.ops.yaml"):
                return httpx.Response(200, text=self.ops_yaml)
            return httpx.Response(200, text=self.changelog)

        if request.method == "GET" and "/releases/tags/" in path:
            if self.release is None:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json=self.release)

        if request.method == "GET" and path.endswith("/releases"):
            return httpx.Response(
                200, json=[] if self.release is None else [self.release]
            )

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
            name = request.url.params.get("name")
            self.uploaded.append(name)
            asset = self._asset(name, request.content)
            self.release = {
                **(self.release or {}),
                "assets": [*((self.release or {}).get("assets") or []), asset],
            }
            return httpx.Response(201, json=asset)

        if request.method == "POST" and path.endswith("/releases"):
            if self.release is not None:
                # Forgejo answers 409 for an existing row (REA-011).
                return httpx.Response(409, json={"message": "exists"})
            payload = json.loads(request.content)
            self.created.append(payload)
            self.release = {
                "id": RELEASE_ID,
                "tag_name": payload.get("tag_name"),
                "name": payload.get("name"),
                "body": payload.get("body") or "",
                "draft": payload.get("draft", False),
                "prerelease": payload.get("prerelease", False),
                "created_at": _now_iso(),
                "assets": [],
            }
            return httpx.Response(201, json=self.release)

        return httpx.Response(
            500, json={"message": f"unexpected {request.method} {path}"}
        )


def _adapter(forge: str, dest: FakeForge):
    if forge == "forgejo":
        adapter = ForgejoAdapter(
            base_url="https://forge.invalid", token="t", webhook_secret="s"
        )
        adapter._client = httpx.AsyncClient(
            base_url="https://forge.invalid/api/v1",
            transport=httpx.MockTransport(dest.handler),
        )
        return adapter
    adapter = GithubAdapter(token="t", webhook_secret="s")
    adapter._client = httpx.AsyncClient(
        base_url="https://api.github.invalid",
        transport=httpx.MockTransport(dest.handler),
    )
    return adapter


class _ManualChannels:
    """A read-only channel probe that proves nothing (all channels manual).

    Hermetic stand-in for the shipped ``HttpxChannelReader``: returning ``None``
    marks every channel manual, which the engine never counts as a failure.
    """

    async def present_at(self, channel, name, version):
        return None


def _fixture_text(shape: str) -> str:
    return (FIXTURES / SHAPES[shape]["filename"]).read_text(encoding="utf-8")


def _declared_assets(shape: str, repo_dir: Path) -> list[str]:
    repo = SHAPES[shape]["repo"]
    return _declared_release_asset_names(
        _shape_config(repo), repo, repo_dir, TAG
    )


def _write_artifacts(repo_dir: Path, names: list[str]) -> Path:
    dist = repo_dir / "dist"
    dist.mkdir(exist_ok=True)
    for name in names:
        (dist / name).write_bytes(f"built bytes of {name}\n".encode())
    return dist


def _write_notes(repo_dir: Path) -> Path:
    path = repo_dir / "notes.md"
    path.write_text(NOTES, encoding="utf-8")
    return path


def _attach_argv(shape: str, repo_dir: Path, dist: Path) -> list[str]:
    argv = [
        "--repo",
        SHAPES[shape]["repo"],
        "--tag",
        TAG,
        "--artifacts",
        str(dist),
        "--repo-dir",
        str(repo_dir),
        "--notes-file",
        str(_write_notes(repo_dir)),
    ]
    if SHAPES[shape]["name_template"] is not None:
        # The release workflow invokes the command with the repository's
        # declared title form; the attach path does not read it from .ops.yaml.
        argv += ["--name-template", SHAPES[shape]["name_template"]]
    return argv


def _layover_body(shape: str, repo_dir: Path, dist: Path, monkeypatch) -> str:
    """Compose the body through the layover path and return it byte for byte.

    Drives ``ReleaseHandler.process_event`` exactly as the deployed release
    workflow does, against a fresh fake whose by-tag lookup is empty, so the
    body the layover composed is the one the create call carries. The tag
    identity write is stubbed: the parity subject is the body, not the tag.
    """
    repo = SHAPES[shape]["repo"]
    ops_yaml = _fixture_text(shape)
    forgejo = FakeForge(forge="forgejo", changelog=CHANGELOG_TEXT, ops_yaml=ops_yaml)
    github = FakeForge(forge="github", changelog=CHANGELOG_TEXT, ops_yaml=ops_yaml)
    adapters = [_adapter("forgejo", forgejo), _adapter("github", github)]

    monkeypatch.setattr(
        release_module, "push_canonical_tag", AsyncMock()
    )

    context = ReleaseContext(
        ops_config=_shape_config(repo),
        artifact_dir=dist,
        identity=TagIdentitySpec(
            repo_dir=str(repo_dir),
            canonical_url=f"{CANONICAL_HOST}/{repo}.git",
            canonical_token="tok",
            mirror_remote="https://mirror.example/repo.git",
        ),
        adapters=adapters,
        channel_reader=_ManualChannels(),
    )

    asyncio.run(
        ReleaseHandler.process_event(
            adapters[0],
            {
                "event_type": "push",
                "action": None,
                "repo": repo,
                "raw": {"ref": f"refs/tags/{TAG}"},
            },
            ReleaseConfig(enabled=True, trigger="tag_push", tag_pattern="v*"),
            context,
        )
    )

    assert forgejo.created, "the layover did not create the Forgejo release"
    assert github.created, "the layover did not create the GitHub release"
    body = forgejo.created[0]["body"]
    # The layover writes the same body to every release destination.
    assert github.created[0]["body"] == body
    return body


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_layover_body_and_attach_body_agree(shape, tmp_path, monkeypatch, capsys):
    """One shape: the layover composition and the attach composition are equal.

    Composes the body through the layover path (``_release_tag``), then runs
    the shipped command with ``--notes-file`` only against fresh fakes and
    asserts the body it creates is the same bytes. A second run against the
    now-existing release proves the command neither re-composes nor rewrites:
    the stored body is untouched and nothing is patched, created or uploaded.
    """
    repo = SHAPES[shape]["repo"]

    # The fixture is a verbatim copy with the source commit SHA in its header.
    first_line = _fixture_text(shape).splitlines()[0]
    assert first_line == (
        f"# Source: {repo} (branch main, commit {SHAPES[shape]['sha']})"
    )

    # The attach command reads .ops.yaml from --repo-dir; the fixture is copied
    # there byte for byte (its comment header is valid YAML).
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / ".ops.yaml").write_text(_fixture_text(shape), encoding="utf-8")
    declared = _declared_assets(shape, repo_dir)
    dist = _write_artifacts(repo_dir, declared)

    monkeypatch.setenv("FORGEJO_BASE_URL", CANONICAL_HOST)
    # Keep the channel probes hermetic; manual channels never fail a release.
    monkeypatch.setattr(release_module, "HttpxChannelReader", _ManualChannels)

    layover_body = _layover_body(shape, repo_dir, dist, monkeypatch)

    # Attach path: a fresh pair of fakes, no release yet.
    forgejo = FakeForge(forge="forgejo")
    github = FakeForge(forge="github")
    attach_adapters = [_adapter("forgejo", forgejo), _adapter("github", github)]

    argv = _attach_argv(shape, repo_dir, dist)
    code = main(argv, adapters=attach_adapters)
    payload = json.loads(capsys.readouterr().out)

    assert code == 0 and payload["ok"] is True, payload
    assert payload["destinations"], "the command touched no destination"
    # The attach composition is the layover composition, byte for byte, on
    # every destination.
    assert forgejo.created and github.created
    assert forgejo.created[0]["body"] == layover_body
    assert github.created[0]["body"] == layover_body

    created = (len(forgejo.created), len(github.created))
    uploaded = (len(forgejo.uploaded), len(github.uploaded))

    # A second run over the existing release converges without overwriting it:
    # D1 never rewrites a differing body and here nothing differs.
    code2 = main(argv, adapters=attach_adapters)
    payload2 = json.loads(capsys.readouterr().out)

    assert code2 == 0 and payload2["ok"] is True, payload2
    assert forgejo.release["body"] == layover_body
    assert github.release["body"] == layover_body
    assert forgejo.patched == [] and github.patched == []
    assert (len(forgejo.created), len(github.created)) == created
    assert (len(forgejo.uploaded), len(github.uploaded)) == uploaded


def test_the_derivation_uses_the_declared_canonical_host(tmp_path, monkeypatch):
    """RB-003 criterion 2: RB-002's derivation reproduces the layover link.

    The finding is that the layover's canonical host (``git.langevc.com``) and
    the runner's ``FORGEJO_BASE_URL`` (``http://forgejo:3000``) differ. Set to
    the declared canonical host, the derivation builds exactly the layover's
    ``<host>/<repo>/src/tag/<tag>/<changelog_path>``; set to the runner address
    it builds that address instead — proving the two hosts are not the same and
    that the parity tests' choice of ``FORGEJO_BASE_URL`` is what makes the link
    agree.
    """
    repo = SHAPES["skillweave-sdk"]["repo"]
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / ".ops.yaml").write_text(
        _fixture_text("skillweave-sdk"), encoding="utf-8"
    )
    (repo_dir / "dist").mkdir()

    def derive(base_url: str) -> str:
        monkeypatch.setenv("FORGEJO_BASE_URL", base_url)
        args = _parser().parse_args(
            [
                "--repo",
                repo,
                "--tag",
                TAG,
                "--artifacts",
                str(repo_dir / "dist"),
                "--repo-dir",
                str(repo_dir),
                "--notes-file",
                str(_write_notes(repo_dir)),
            ]
        )
        return request_from_args(args).changelog_url

    expected = f"{CANONICAL_HOST}/{repo}/src/tag/{TAG}/CHANGELOG.md"
    assert derive(CANONICAL_HOST) == expected

    # The runner's github.server_url names a different host, so the derivation
    # follows it there — the measured mismatch, recorded here as the proof.
    runner_url = f"{RUNNER_BASE_URL}/{repo}/src/tag/{TAG}/CHANGELOG.md"
    assert derive(RUNNER_BASE_URL) == runner_url
    assert runner_url != expected
