"""OE-042b: the Create Release step supplements a release that already exists.

The test executes the ``run:`` block of the step ``Create Release`` taken from
``.forgejo/workflows/forgejo-release.yml``; it holds no copy of the step. Only
the network and the processes the block starts are replaced:

* ``python3 -m venv`` and ``pip install``: a ``python3`` double on PATH that
  creates a venv whose ``python`` runs the interpreter of the test environment
  (``ops_engine`` of the tree under test is put on its PYTHONPATH) and answers
  ``-m pip`` with success.
* ``git push``, ``git ls-remote``, ``git rev-parse``: a ``git`` double on PATH
  that answers as a real tag would (the same commit on both forges).
* the two forges: local HTTP servers that answer the calls the adapters make.
  The GitHub adapter has fixed hosts, so a ``sitecustomize`` on the block's
  PYTHONPATH points ``API_BASE`` and ``UPLOADS_HOST`` at the GitHub double;
  nothing else of the adapter or the engine is touched.

The release the Forgejo double holds is the bot release of v3.5.1. It is
RECONSTRUCTED from the owner's screenshot, not measured: the real object was
deleted before it was saved (title in small letters, no attached files, the
notes of the build). Ids and timestamps are not known and are made up here.
"""

import json
import os
import re
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".forgejo" / "workflows" / "forgejo-release.yml"
STEP_NAME = "Create Release"

TAG = "v3.5.1"
FORGEJO_REPO = "langevc/ops-engine"
GITHUB_REPO = "LangeVC/ops-engine"
BOT_TITLE = "ops-engine v3.5.1"
GATED_TITLE = "Ops Engine v3.5.1"
TAG_COMMIT = "6a0b8aee423a72904ae3b97399e1615293c47d6a"
NOTES = (
    "A release that already existed on a destination could be reported as a "
    "failure, and the dependency scan could stop a release over a finding that "
    "was not serious. Both now behave as intended.\n\n"
    "### Existing releases are recognised\n\n"
    "- **A destination that already holds the release is left alone.** A create "
    "call answered with 409 (Forgejo) or 422 (GitHub) means the release exists.\n"
)
FILES = {
    "ops_engine-3.5.1-py3-none-any.whl": b"wheel bytes",
    "ops_engine-3.5.1.tar.gz": b"sdist bytes",
    "SHA256SUMS": b"sums\n",
    "sbom.cdx.json": b"{}",
}

OPS_YAML = f"""\
destinations:
  - forge: forgejo
    repo: {FORGEJO_REPO}
    role: release
    visibility: public
    credential_variable: FORGEJO_TOKEN
  - forge: github
    repo: {GITHUB_REPO}
    role: release
    visibility: public
    credential_variable: GH_MIRROR_TOKEN
"""


def _step() -> dict:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == STEP_NAME:
                return step
    raise AssertionError(f"step {STEP_NAME!r} not found in {WORKFLOW}")


class Forge:
    """A local HTTP server that holds releases the way a forge does.

    ``releases`` maps a tag to the release object. ``hidden_until_create`` makes
    the by-tag lookup and the list answer as if the release did not exist until
    the create call has been answered (the race: the bot creates the release
    between the existence check and the create). ``create_answers`` is the
    status the create call gives when the release row exists or is about to.
    """

    def __init__(self, kind: str, release: dict | None = None, *, race: bool = False):
        self.kind = kind
        self.releases: dict[str, dict] = {}
        if release is not None:
            self.releases[TAG] = dict(release)
        self.race = race
        self.created_seen = False
        self.requests: list[tuple[str, str]] = []
        self.uploads: list[str] = []
        self.patches: list[dict] = []
        self.next_id = 100
        self._server: ThreadingHTTPServer | None = None

    # -- request handling -------------------------------------------------
    def _visible(self) -> bool:
        return not (self.race and not self.created_seen)

    def handle(self, method: str, path: str, query: dict, body: bytes):
        self.requests.append((method, path))
        rel = re.fullmatch(r"/(?:api/v1/)?repos/([^/]+/[^/]+)/releases(.*)", path)
        if rel is None:
            return 404, {"message": f"double: unexpected {method} {path}"}
        rest = rel.group(2)
        if method == "GET" and rest == f"/tags/{TAG}":
            if TAG in self.releases and self._visible():
                return 200, self.releases[TAG]
            return 404, {"message": "release not found"}
        if method == "GET" and rest == "":
            rows = [self.releases[TAG]] if TAG in self.releases and self._visible() else []
            return 200, rows if query.get("page", ["1"])[0] == "1" else []
        if method == "POST" and rest == "":
            self.created_seen = True
            if TAG in self.releases:
                return (409 if self.kind == "forgejo" else 422), {
                    "message": "release exists"
                }
            data = json.loads(body)
            self.next_id += 1
            self.releases[TAG] = {
                "id": self.next_id,
                "tag_name": TAG,
                "name": data["name"],
                "body": data["body"],
                "assets": [],
            }
            return 201, self.releases[TAG]
        m = re.fullmatch(r"/(\d+)", rest)
        if method == "PATCH" and m:
            release = self.releases[TAG]
            patch = json.loads(body)
            self.patches.append(patch)
            release.update(patch)
            return 200, release
        m = re.fullmatch(r"/(\d+)/assets", rest)
        if method == "POST" and m:
            name = query["name"][0]
            self.uploads.append(name)
            asset = {"id": len(self.uploads), "name": name, "size": len(body)}
            self.releases[TAG].setdefault("assets", []).append(asset)
            return 201, asset
        return 404, {"message": f"double: unexpected {method} {path}"}

    # -- server -----------------------------------------------------------
    def start(self) -> str:
        forge = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self):
                url = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                status, payload = forge.handle(
                    self.command, url.path, parse_qs(url.query), body
                )
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PATCH = _serve

            def log_message(self, *args):  # keep the test output quiet
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


def bot_release() -> dict:
    """The reconstructed bot release: small-letter title, no files, the notes."""
    return {
        "id": 41,
        "tag_name": TAG,
        "target_commitish": TAG_COMMIT,
        "name": BOT_TITLE,
        "body": NOTES,
        "draft": False,
        "prerelease": False,
        "author": {"login": "dev.bot_langevc"},
        "assets": [],
    }


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def run_step(tmp_path: Path, forgejo: Forge, github: Forge) -> subprocess.CompletedProcess:
    """Run the workflow's own Create Release block against the two doubles."""
    work = tmp_path / "work"
    bin_dir = tmp_path / "bin"
    shim = tmp_path / "shim"
    for d in (work, bin_dir, shim, work / "dist"):
        d.mkdir(parents=True, exist_ok=True)
    (work / "dist" / "ops_engine-3.5.1-py3-none-any.whl").write_bytes(
        FILES["ops_engine-3.5.1-py3-none-any.whl"]
    )
    (work / "dist" / "ops_engine-3.5.1.tar.gz").write_bytes(FILES["ops_engine-3.5.1.tar.gz"])
    (work / "SHA256SUMS").write_bytes(FILES["SHA256SUMS"])
    (work / "sbom.cdx.json").write_bytes(FILES["sbom.cdx.json"])
    (work / ".ops.yaml").write_text(OPS_YAML, encoding="utf-8")
    (work / "constraints.txt").write_text("", encoding="utf-8")

    real_python = sys.executable
    # python3: `-m venv DIR` makes a venv whose python is the test interpreter,
    # and `-m pip ...` there succeeds without installing anything.
    _executable(
        bin_dir / "python3",
        'if [ "${1:-}" = "-m" ] && [ "${2:-}" = "venv" ]; then\n'
        '  mkdir -p "$3/bin"\n'
        '  printf \'#!/usr/bin/env bash\\n'
        'if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ]; then exit 0; fi\\n'
        f'exec "{real_python}" "$@"\\n\' > "$3/bin/python"\n'
        '  chmod +x "$3/bin/python"\n'
        "  exit 0\n"
        "fi\n"
        f'exec "{real_python}" "$@"\n',
    )
    # git: the canonical tag and the mirror tag peel to the same commit.
    _executable(
        bin_dir / "git",
        'case "${1:-}" in\n'
        "  push) exit 0 ;;\n"
        f'  ls-remote) printf \'{TAG_COMMIT}\\trefs/tags/{TAG}^{{}}\\n\'; exit 0 ;;\n'
        f"  rev-parse) echo {TAG_COMMIT}; exit 0 ;;\n"
        '  *) echo "git double: unexpected: $*" >&2; exit 97 ;;\n'
        "esac\n",
    )
    github_url = github.start()
    forgejo_url = forgejo.start()
    (shim / "sitecustomize.py").write_text(
        "import os\n"
        "try:\n"
        "    import ops_engine.adapters.github_adapter as g\n"
        "    g.API_BASE = os.environ['TEST_GITHUB_API']\n"
        "    g.UPLOADS_HOST = os.environ['TEST_GITHUB_API']\n"
        "except ImportError:\n"
        "    pass\n",
        encoding="utf-8",
    )

    step_env = dict(_step().get("env", {}))
    env = {
        # the values the workflow's own env block holds as literals
        "NAME_TEMPLATE": step_env["NAME_TEMPLATE"],
        # the values the runner supplies
        "TAG_NAME": TAG,
        "NOTES": NOTES,
        "FORGEJO_REPOSITORY": FORGEJO_REPO,
        "FORGEJO_BASE_URL": forgejo_url,
        "FORGEJO_TOKEN": "test-token-forgejo",
        "FORGEJO_WEBHOOK_SECRET": "test-secret-forgejo",
        "GH_MIRROR_TOKEN": "test-token-github",
        "GH_MIRROR_WEBHOOK_SECRET": "test-secret-github",
        "TEST_GITHUB_API": github_url,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "PYTHONPATH": os.pathsep.join([str(shim), str(REPO_ROOT / "src")]),
    }
    try:
        return subprocess.run(
            ["bash", "-eo", "pipefail", "-c", _step()["run"]],
            check=False,
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        forgejo.stop()
        github.stop()


def log_of(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout + proc.stderr


def test_a_bot_release_gets_the_gated_title_and_the_four_files(tmp_path):
    # RED at base 6a0b8aee: the step does not pass skip_existing, so the
    # create call answers 409 and the destination fails (PartialPublicationError)
    # with the title unchanged and nothing uploaded.
    forgejo = Forge("forgejo", bot_release())
    github = Forge("github")
    proc = run_step(tmp_path, forgejo, github)
    log = log_of(proc)

    assert proc.returncode == 0, log
    release = forgejo.releases[TAG]
    assert release["name"] == GATED_TITLE
    assert sorted(a["name"] for a in release["assets"]) == sorted(FILES)
    assert sorted(forgejo.uploads) == sorted(FILES)
    assert release["body"] == NOTES
    assert re.search(
        rf"^supplemented destinations:\s+forgejo:{re.escape(FORGEJO_REPO)}$",
        log,
        re.MULTILINE,
    ), log
    assert re.search(
        rf"^published destinations:\s+github:{re.escape(GITHUB_REPO)}$",
        log,
        re.MULTILINE,
    ), log
    assert re.search(r"^skipped destinations:\s+\(none\)$", log, re.MULTILINE), log
    assert re.search(r"^failed destinations:\s+\(none\)$", log, re.MULTILINE), log
    assert "PartialPublicationError" not in log


def test_the_race_with_the_bot_is_supplemented_through_the_real_block(tmp_path):
    # The by-tag lookup and the list answer 404 until the create call; the create
    # call answers 409 (the bot made the release in between) and the release is
    # served from then on. RED at base 6a0b8aee: the 409 fails the destination
    # and the run exits 1 with the title unchanged and nothing uploaded.
    forgejo = Forge("forgejo", bot_release(), race=True)
    github = Forge("github")
    proc = run_step(tmp_path, forgejo, github)
    log = log_of(proc)

    assert proc.returncode == 0, log
    assert ("POST", f"/api/v1/repos/{FORGEJO_REPO}/releases") in forgejo.requests
    release = forgejo.releases[TAG]
    assert release["name"] == GATED_TITLE
    assert sorted(a["name"] for a in release["assets"]) == sorted(FILES)
    assert re.search(
        rf"^supplemented destinations:\s+forgejo:{re.escape(FORGEJO_REPO)}$",
        log,
        re.MULTILINE,
    ), log
    assert re.search(r"^failed destinations:\s+\(none\)$", log, re.MULTILINE), log


def test_different_notes_stay_an_error_through_the_real_block(tmp_path):
    # An existing release with other notes is not this build's: the run exits
    # non-zero, changes no title, uploads nothing, and the log names the
    # difference and the existing release id. RED at base 6a0b8aee: the run
    # exits 1 as well, but prints neither the difference nor the id.
    other = bot_release()
    other["body"] = "Notes somebody else wrote for this tag.\n"
    forgejo = Forge("forgejo", other)
    github = Forge("github")
    proc = run_step(tmp_path, forgejo, github)
    log = log_of(proc)

    assert proc.returncode != 0, log
    assert forgejo.uploads == []
    assert forgejo.patches == []
    release = forgejo.releases[TAG]
    assert release["name"] == BOT_TITLE
    assert release["assets"] == []
    assert release["body"] == other["body"]
    assert "notes differ from the notes of this build" in log, log
    assert "existing_release_id=41" in log, log
    assert re.search(
        rf"^failed destinations:\s+forgejo:{re.escape(FORGEJO_REPO)}$",
        log,
        re.MULTILINE,
    ), log
    assert re.search(r"^supplemented destinations:\s+\(none\)$", log, re.MULTILINE), log
