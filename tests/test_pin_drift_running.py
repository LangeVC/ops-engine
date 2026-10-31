"""pin-drift-check, running-version leg (OE-002, D16).

A layover register entry may carry ``health_url``. The check fetches that URL,
reads the ``version`` key of the JSON answer (the ``version`` field of
RuntimeIdentity) and compares it with the layover's declared pin:

- equal            -> reported, exit 0
- different        -> exit 1, both versions named
- unreachable / not an identity answer -> INCONCLUSIVE, never a pass (exit 3)

Real HTTP against a throwaway local server; nothing leaves the machine.
"""

import importlib.util
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "pin-drift-check.py"

spec = importlib.util.spec_from_file_location("pin_drift_check", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

ENGINE_VERSION = mod.read_latest_version(REPO)


@pytest.fixture
def health_server():
    """Serve ``/health`` with whatever ``server.body`` / ``server.status`` say."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(self.server.status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(self.server.body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.status = 200
    server.body = b"{}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.url = f"http://127.0.0.1:{server.server_port}/health"
    yield server
    server.shutdown()
    server.server_close()


def _register(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "register.json"
    path.write_text(
        json.dumps({"schema": 1, "package": "ops_engine", "layovers": entries}),
        encoding="utf-8",
    )
    return path


def _run(register: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--layovers", str(register), "--repo", str(REPO)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_matching_running_version_is_reported_and_exits_zero(tmp_path, health_server):
    health_server.body = json.dumps(
        {"version": ENGINE_VERSION, "config_sha256": "x"}
    ).encode()
    reg = _register(
        tmp_path,
        [{"name": "a-ops", "pin": ENGINE_VERSION, "health_url": health_server.url}],
    )
    r = _run(reg)
    assert r.returncode == 0, r.stderr
    assert "a-ops" in r.stdout
    assert ENGINE_VERSION in r.stdout
    assert "running" in r.stdout


def test_report_lists_declared_pin_and_running_version_per_layover(
    tmp_path, health_server
):
    health_server.body = json.dumps({"version": ENGINE_VERSION}).encode()
    reg = _register(
        tmp_path,
        [
            {"name": "a-ops", "pin": ENGINE_VERSION, "health_url": health_server.url},
            {"name": "b-ops", "pin": f"v{ENGINE_VERSION}", "health_url": health_server.url},
        ],
    )
    r = _run(reg)
    assert r.returncode == 0, r.stderr
    for name in ("a-ops", "b-ops"):
        line = next(l for l in r.stdout.splitlines() if l.startswith(name))
        assert line.count(ENGINE_VERSION) >= 2  # declared pin and running version


def test_differing_running_version_exits_nonzero_and_names_both(
    tmp_path, health_server
):
    health_server.body = json.dumps({"version": "0.0.1"}).encode()
    reg = _register(
        tmp_path,
        [{"name": "a-ops", "pin": ENGINE_VERSION, "health_url": health_server.url}],
    )
    r = _run(reg)
    assert r.returncode == 1
    assert "a-ops" in r.stderr
    assert "0.0.1" in r.stderr
    assert ENGINE_VERSION in r.stderr


def test_unreachable_health_is_inconclusive_and_never_a_pass(tmp_path, health_server):
    url = health_server.url
    health_server.shutdown()
    health_server.server_close()
    reg = _register(
        tmp_path, [{"name": "a-ops", "pin": ENGINE_VERSION, "health_url": url}]
    )
    r = _run(reg)
    assert r.returncode != 0
    assert "INCONCLUSIVE" in r.stderr + r.stdout
    assert "a-ops" in r.stderr + r.stdout


@pytest.mark.parametrize(
    "status,body",
    [
        (500, b'{"version": "' + b"9.9.9" + b'"}'),
        (200, b"not json"),
        (200, b'{"config_sha256": "x"}'),
        (200, b'["version"]'),
        (200, b'{"version": ""}'),
    ],
)
def test_unusable_answer_is_inconclusive(tmp_path, health_server, status, body):
    health_server.status = status
    health_server.body = body
    reg = _register(
        tmp_path,
        [{"name": "a-ops", "pin": ENGINE_VERSION, "health_url": health_server.url}],
    )
    r = _run(reg)
    assert r.returncode != 0
    assert "INCONCLUSIVE" in r.stderr + r.stdout


def test_unknown_runtime_version_is_inconclusive(tmp_path, health_server):
    health_server.body = json.dumps({"version": "unknown"}).encode()
    reg = _register(
        tmp_path,
        [{"name": "a-ops", "pin": ENGINE_VERSION, "health_url": health_server.url}],
    )
    r = _run(reg)
    assert r.returncode != 0
    assert "INCONCLUSIVE" in r.stderr + r.stdout


def test_drift_outranks_inconclusive(tmp_path, health_server):
    health_server.body = json.dumps({"version": "0.0.1"}).encode()
    reg = _register(
        tmp_path,
        [
            {"name": "a-ops", "pin": ENGINE_VERSION, "health_url": health_server.url},
            {"name": "b-ops", "pin": ENGINE_VERSION, "health_url": "http://127.0.0.1:1/health"},
        ],
    )
    r = _run(reg)
    assert r.returncode == 1
    assert "0.0.1" in r.stderr
    assert "INCONCLUSIVE" in r.stderr + r.stdout


def test_register_without_health_url_keeps_pin_only_behaviour(tmp_path):
    reg = _register(tmp_path, [{"name": "a-ops", "pin": ENGINE_VERSION}])
    r = _run(reg)
    assert r.returncode == 0
    assert "INCONCLUSIVE" not in r.stderr + r.stdout


def test_non_string_health_url_is_a_named_refusal(tmp_path):
    reg = _register(
        tmp_path, [{"name": "a-ops", "pin": ENGINE_VERSION, "health_url": 5}]
    )
    r = _run(reg)
    assert r.returncode == 2
    assert "health_url" in r.stderr
    assert "Traceback" not in r.stderr


def test_script_docstring_documents_both_names():
    doc = mod.__doc__
    assert "health_url" in doc
    assert "version" in doc and "RuntimeIdentity" in doc


BASE_SHA = "b1dbfd922741330f34dac45dc3b9f9bb5a2da619"  # last commit before OE-002


def _pin_only_script_at_base(tmp_path: Path) -> Path | None:
    """The pin-only check as it was at the base commit, or None if unavailable."""
    shown = subprocess.run(
        ["git", "-C", str(REPO), "show", f"{BASE_SHA}:scripts/pin-drift-check.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    if shown.returncode != 0:
        return None
    path = tmp_path / "pin-drift-check-base.py"
    path.write_text(shown.stdout, encoding="utf-8")
    return path


def test_red_proof_older_running_version_fails_where_pin_only_passes(
    tmp_path, health_server
):
    """RED proof: the declared pin is current, the layover actually runs older.

    The pin-only check (the same register without ``health_url``, and the
    script as it was at the base commit) passes this fixture; the running-version
    check exits non-zero.
    """
    health_server.body = json.dumps({"version": "0.0.1", "config_sha256": "x"}).encode()
    entry = {"name": "a-ops", "pin": ENGINE_VERSION}
    with_health = _register(tmp_path, [{**entry, "health_url": health_server.url}])
    pin_only = tmp_path / "pin-only.json"
    pin_only.write_text(
        json.dumps({"schema": 1, "package": "ops_engine", "layovers": [entry]}),
        encoding="utf-8",
    )

    assert _run(pin_only).returncode == 0  # pin-only behaviour passes the fixture
    checked = _run(with_health)
    assert checked.returncode != 0
    assert "0.0.1" in checked.stderr

    base_script = _pin_only_script_at_base(tmp_path)
    if base_script is not None:
        base = subprocess.run(
            [sys.executable, str(base_script), "--layovers", str(with_health),
             "--repo", str(REPO)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert base.returncode == 0, base.stderr  # the base check cannot see it
