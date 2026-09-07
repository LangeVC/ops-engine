"""ADP-010 — Layer 1 knows no organisation and no CI system, and a gate keeps it.

This file proves the Layer-1 boundary twice over: (1) the engine module
``ops_engine.modules.health_monitor`` no longer names an organisation by value
and no longer reads a CI environment variable, taking ``repo``/``token``/
``namespace`` as arguments instead, and (2) ``scripts/ci_variable_boundary_gate.py``
refuses either shape when reintroduced, using ``ast`` (not ``re``) so the
distinction between a documentation mention and a live value is a parse node,
with the vocabulary supplied from outside and none shipped in the gate.

The two REAL red cases are taken from git history: the rate-limit decorator
``@track_rate_limit(namespace="capacium-ops")`` (commit ``e822d86``) and the
environment reads ``os.environ.get("GITHUB_REPOSITORY")`` /
``os.environ.get("GITHUB_TOKEN")`` (commit ``1509f20``) — both in
``health_monitor.py``. The tests run the committed gate as a subprocess, never
by reading the gate's source.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The source tree must win over any installed ops-engine so the run() test
# exercises the committed module rather than a site-packages copy, both under
# pytest (which sets pythonpath=src) and under the standalone harness below.
_SRC = str(REPO_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

GATE = REPO_ROOT / "scripts" / "ci_variable_boundary_gate.py"
HEALTH_MONITOR = (
    REPO_ROOT / "src" / "ops_engine" / "modules" / "health_monitor.py"
)

# REAL red-proof bytes, copied faithfully from git history (see module docstring).
# ``capacium-ops`` is the organisation name baked into the decorator; the two
# ``os.environ.get`` calls are the direct CI-environment reads.
RED_PROOF_HEALTH_MONITOR = """\
import os


@track_rate_limit(namespace="capacium-ops")
def _emit_github_issue(results, sink, repo, token):
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    return repo + token
"""

# The same organisation name in a DOCSTRING (and a comment) is documentation.
DOCSTRING_ONLY = '''\
"""This module documents the organisation capacium-ops as an example only."""
# a comment naming capacium-ops is documentation too


def helper():
    """A function docstring mentioning capacium-ops."""
    return 1
'''

# The same organisation name as a LIVE value.
LIVE_VALUE = '''\
org = "capacium-ops"
'''


def _write(tmp, text, name):
    path = Path(tmp) / name
    path.write_text(text, encoding="utf-8")
    return path


def _vocab(tmp, terms, name="vocab.txt"):
    path = Path(tmp) / name
    path.write_text("\n".join(terms) + "\n", encoding="utf-8")
    return path


def _run_py_scan(py_dir, org_vocab=None, ci_env=None):
    argv = [sys.executable, str(GATE), "--py-dir", str(py_dir)]
    if org_vocab is not None:
        argv += ["--org-vocab", str(org_vocab)]
    if ci_env is not None:
        argv += ["--ci-env", str(ci_env)]
    return subprocess.run(argv, capture_output=True, text=True)


# ── Criterion 1: both live violations gone, health monitor works end to end ──


def test_health_monitor_source_has_no_org_literal():
    """The namespace literal ``capacium-ops`` is gone from executing code (the
    module names no organisation by value)."""
    text = HEALTH_MONITOR.read_text(encoding="utf-8")
    assert '@track_rate_limit(namespace="capacium-ops")' not in text
    # The DocString still shows the CLI surface; nothing names an org by value.
    assert "namespace=capacium-ops" not in text
    assert "capacium-ops" not in text


def test_health_monitor_source_reads_no_ci_environment():
    """Neither GITHUB_REPOSITORY nor GITHUB_TOKEN is read from the environment."""
    text = HEALTH_MONITOR.read_text(encoding="utf-8")
    assert 'os.environ.get("GITHUB_REPOSITORY")' not in text
    assert 'os.environ.get("GITHUB_TOKEN")' not in text
    assert "GITHUB_REPOSITORY" not in text
    assert "GITHUB_TOKEN" not in text


def _load_hm():
    import ops_engine.modules.health_monitor as hm

    return hm


def test_run_supplies_repo_token_namespace_as_arguments():
    """HealthMonitor.run threads the caller-supplied repo/token/namespace into the
    github_issue sink as arguments — a real run of run(), not a file read."""
    from ops_engine.config_loader import HealthCheck, HealthMonitorConfig, HealthSink

    hm = _load_hm()
    config = HealthMonitorConfig(
        enabled=True,
        checks=[HealthCheck(name="c", url="http://example.invalid/")],
        sinks=[HealthSink(type="github_issue")],
    )
    captured = {}

    def _fake_probe(check):
        return {"name": check.name, "url": check.url, "ok": True}

    def _fake_emit(results, sink, repo, token, namespace):
        captured["repo"] = repo
        captured["token"] = token
        captured["namespace"] = namespace

    try:
        orig_probe = hm._probe
        orig_emit = hm._emit_github_issue
        hm._probe = _fake_probe
        hm._emit_github_issue = _fake_emit
        rc = hm.HealthMonitor.run(
            config, repo="Owner/Repo", token="tok", namespace="ns"
        )
    finally:
        hm._probe = orig_probe
        hm._emit_github_issue = orig_emit

    assert rc == 0
    assert captured == {"repo": "Owner/Repo", "token": "tok", "namespace": "ns"}


def test_health_monitor_cli_runs_end_to_end_without_ci_environment():
    """The CLI loads a config and runs to completion, supplying no environment,
    when the config has no checks — a real subprocess run of the module."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.yml"
        cfg.write_text(
            "capacium:\n  health_monitor:\n    enabled: true\n    checks: []\n",
            encoding="utf-8",
        )
        import os as _os

        env = dict(_os.environ)
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            str(REPO_ROOT / "src") if not existing else str(REPO_ROOT / "src") + ":" + existing
        )
        r = subprocess.run(
            [
                sys.executable,
                "-m",
                "ops_engine.modules.health_monitor",
                "--config",
                str(cfg),
                "--repo",
                "Owner/Repo",
                "--token",
                "tok",
                "--rate-limit-namespace",
                "ns",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=env,
        )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "disabled or no checks" in r.stderr


# ── Criterion 2: the gate refuses both real cases, vocabulary from outside ──


def test_gate_refuses_the_real_org_literal_and_ci_env_reads():
    """The two real cases from git history — the ``capacium-ops`` namespace and
    the ``GITHUB_REPOSITORY``/``GITHUB_TOKEN`` environment reads — are refused,
    naming file and line, with vocabulary supplied from outside the gate."""
    with tempfile.TemporaryDirectory() as tmp:
        py_dir = Path(tmp) / "py"
        py_dir.mkdir()
        _write(tmp, RED_PROOF_HEALTH_MONITOR, "py/health_monitor.py")
        org = _vocab(tmp, ["capacium-ops"], name="org.txt")
        ci = _vocab(tmp, ["GITHUB_REPOSITORY", "GITHUB_TOKEN"], name="ci.txt")
        r = _run_py_scan(py_dir, org_vocab=org, ci_env=ci)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "capacium-ops" in r.stderr
    assert "GITHUB_REPOSITORY" in r.stderr
    assert "GITHUB_TOKEN" in r.stderr
    assert "Layer1BoundaryError" in r.stderr
    # The organisation name is named on the decorator line; the env reads follow.
    assert ":4:" in r.stderr or ":5:" in r.stderr or ":6:" in r.stderr


def test_no_vocabulary_refuses_nothing():
    """The engine ships no organisation vocabulary: with neither flag the scan
    refuses nothing, exactly like REL-011's absent-vocabulary default."""
    with tempfile.TemporaryDirectory() as tmp:
        py_dir = Path(tmp) / "py"
        py_dir.mkdir()
        _write(tmp, RED_PROOF_HEALTH_MONITOR, "py/health_monitor.py")
        r = _run_py_scan(py_dir)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout


def test_gate_is_green_against_the_fixed_tree():
    """The fixed src/ (no org literal, no CI env read in executing code) passes
    the gate with the committed vocabulary."""
    with tempfile.TemporaryDirectory() as tmp:
        org = _vocab(
            tmp, ["capacium-ops", "Capacium", "capacium", "LangeVC", "langevc", "lvc-ops"], name="org.txt"
        )
        ci = _vocab(tmp, ["GITHUB_REPOSITORY", "GITHUB_TOKEN"], name="ci.txt")
        r = _run_py_scan(REPO_ROOT / "src", org_vocab=org, ci_env=ci)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout


# ── Criterion 3: documentation vs live is decided by parsing, not pattern ──


def test_same_org_name_passes_in_docstring_and_comment():
    """The SAME organisation name in a module/function docstring and a comment is
    documentation and passes — in one run over the name."""
    with tempfile.TemporaryDirectory() as tmp:
        py_dir = Path(tmp) / "py"
        py_dir.mkdir()
        _write(tmp, DOCSTRING_ONLY, "py/doc.py")
        org = _vocab(tmp, ["capacium-ops"])
        r = _run_py_scan(py_dir, org_vocab=org)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout


def test_same_org_name_is_refused_as_a_live_value():
    """The SAME organisation name as a live string constant is refused — in one
    run over the name, naming file and line."""
    with tempfile.TemporaryDirectory() as tmp:
        py_dir = Path(tmp) / "py"
        py_dir.mkdir()
        _write(tmp, LIVE_VALUE, "py/live.py")
        org = _vocab(tmp, ["capacium-ops"])
        r = _run_py_scan(py_dir, org_vocab=org)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "capacium-ops" in r.stderr
    assert "Layer1BoundaryError" in r.stderr


def test_ci_env_read_from_config_is_not_refused():
    """A read whose variable name comes from configuration
    (``os.environ.get(cfg.var_name)``) is not a literal and is not refused —
    only a literal CI variable name in executing Python is."""
    from_config = """\
import os


def read(cfg):
    return os.environ.get(cfg.var_name)
"""
    with tempfile.TemporaryDirectory() as tmp:
        py_dir = Path(tmp) / "py"
        py_dir.mkdir()
        _write(tmp, from_config, "py/from_config.py")
        ci = _vocab(tmp, ["GITHUB_REPOSITORY", "GITHUB_TOKEN"])
        r = _run_py_scan(py_dir, ci_env=ci)
    assert r.returncode == 0, r.stdout + r.stderr


def _main():
    failures = []
    ran = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            ran += 1
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                failures.append(name)
                sys.stdout.write("FAIL %s: %r\n" % (name, exc))
            else:
                sys.stdout.write("PASS %s\n" % name)
    sys.stdout.write("\n%d run, %d failed\n" % (ran, len(failures)))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    _main()
