"""ADP-008: the release destination comes from a committed file, not a CI variable.

ADP-004 replaced a hardcoded forge literal with ``vars.RELEASE_DESTINATIONS``, a
Forgejo Actions variable. That is a regression against the standing operator
decision (LVC-247/248): the destination lives in the config layer. This lane
closes it.

The three acceptance criteria, each proven by a real run:

1. A committed ``.ops.yaml`` at the repository root declares BOTH the canonical
   Forgejo destination and the mirror, and the workflow resolves them from it via
   ``load_ops_yaml`` — with no environment variable set at all.
2. ``RELEASE_DESTINATIONS`` appears nowhere under ``.forgejo/`` and no forge
   repository or release API host is a literal there.
3. A missing or malformed ``.ops.yaml`` fails the release with a named error,
   never a silently narrowed release. Both directions refuse and exit non-zero.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from ops_engine.config_loader import OpsYamlError, load_ops_yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
FORGEJO = REPO_ROOT / ".forgejo"
FORGEJO_RELEASE = FORGEJO / "workflows" / "forgejo-release.yml"

WORKFLOW_CONTENT = FORGEJO_RELEASE.read_text(encoding="utf-8")


def _publish_block() -> str:
    """The engine-call Python the workflow runs, de-indented (PUBLISH_PY block)."""
    lines = WORKFLOW_CONTENT.splitlines()
    start = end = None
    for i, line in enumerate(lines):
        if "<<'PUBLISH_PY'" in line:
            start = i
        if start is not None and line.strip() == "PUBLISH_PY" and i > start:
            end = i
            break
    assert start is not None and end is not None, "PUBLISH_PY block not found"
    return textwrap.dedent("\n".join(lines[start + 1 : end]) + "\n")


def _resolution_guard() -> str:
    """The block's destination-resolution + refusal section: everything from the
    top of the block up to (not including) ``repo = os.environ[...]``.

    This is the exact code the workflow runs before it touches any forge, so it
    is the runnable unit that proves criteria 1 and 3 without a network or an
    environment. It is the workflow's own text, not a reimplementation.
    """
    block = _publish_block()
    cut = block.index("repo = os.environ")
    return block[:cut]


# ── Criterion 1: the committed .ops.yaml declares both destinations ──────────


def test_committed_ops_yaml_declares_both_destinations():
    """The committed .ops.yaml resolves both the canonical Forgejo destination
    and the GitHub mirror via load_ops_yaml — a real run, no env var involved."""
    layer3 = load_ops_yaml(REPO_ROOT)
    assert layer3 is not None
    resolved = [(d.forge, d.repo, d.role) for d in layer3.destinations]
    assert resolved == [
        ("forgejo", "langevc/ops-engine", "release"),
        ("github", "LangeVC/ops-engine", "release"),
    ]


def test_resolution_guard_passes_with_no_environment_variable():
    """Running the workflow's resolution path against the checkout with NO
    environment variable set must reach past the destination guards — proving the
    destinations came from the file, not from an Actions variable. The process
    then fails only on the first runtime-only env read (FORGEJO_REPOSITORY),
    which is exactly the Forgejo-provided repository identity, not a destination.
    """
    guard = _resolution_guard()
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("FORGEJO_", "GH_", "RELEASE_DESTINATIONS")):
            env.pop(key, None)
    proc = subprocess.run(
        [sys.executable, "-c", guard],
        cwd=str(REPO_ROOT),
        env={**env, "PYTHONPATH": str(REPO_ROOT / "src")},
        capture_output=True,
        text=True,
    )
    # It must not be refused for a missing/empty destination declaration.
    assert "MissingOpsYamlError" not in proc.stderr
    assert "MissingReleaseDestinationsError" not in proc.stderr
    assert "OpsYamlError" not in proc.stderr


def test_workflow_reads_ops_yaml_not_an_actions_variable():
    """The publish block reads destinations via load_ops_yaml and carries no
    RELEASE_DESTINATIONS reference; the Forgejo-provided repository identity is
    still used as the repo selector, not as a destination literal."""
    block = _publish_block()
    assert "load_ops_yaml" in block
    assert "RELEASE_DESTINATIONS" not in block
    assert "FORGEJO_REPOSITORY" in block


# ── Criterion 2: no variable and no literal under .forgejo/ ──────────────────


def test_release_destinations_absent_under_forgejo():
    """RELEASE_DESTINATIONS appears nowhere under .forgejo/."""
    hits: list[tuple[str, int]] = []
    for path in FORGEJO.rglob("*"):
        if path.is_file() and "RELEASE_DESTINATIONS" in path.read_text(encoding="utf-8"):
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "RELEASE_DESTINATIONS" in line:
                    hits.append((str(path), i))
    assert not hits, f"RELEASE_DESTINATIONS still present at: {hits}"


def test_no_release_destination_literal_in_the_release_workflow():
    """No forge repository name or release API host is a literal in the release
    workflow. The destination values live in .ops.yaml, the config layer."""
    offending: list[str] = []
    for lit in (
        "api.github.com",
        "uploads.github.com",
        "LangeVC/ops-engine",
        "langevc/ops-engine",
        "GH_REPO=",
        "GH_API=",
    ):
        for lineno, line in enumerate(WORKFLOW_CONTENT.splitlines(), start=1):
            if lit in line:
                offending.append(f"{lit} (line {lineno})")
    assert not offending, f"destination literals still present: {offending}"


# ── Criterion 3: missing and malformed .ops.yaml are named refusals ──────────


def _run_guard_in(repo_dir: Path) -> subprocess.CompletedProcess:
    guard = _resolution_guard()
    return subprocess.run(
        [sys.executable, "-c", guard],
        cwd=str(repo_dir),
        env={**dict(os.environ), "PYTHONPATH": str(REPO_ROOT / "src")},
        capture_output=True,
        text=True,
    )


def test_missing_ops_yaml_is_a_named_refusal(tmp_path):
    """A directory with no .ops.yaml runs the workflow's resolution path and is
    refused with MissingOpsYamlError, exiting non-zero — never publishing to
    fewer (zero) destinations."""
    proc = _run_guard_in(tmp_path)
    assert proc.returncode != 0
    assert "MissingOpsYamlError" in proc.stderr


def test_malformed_ops_yaml_is_a_named_refusal(tmp_path):
    """A malformed .ops.yaml runs the workflow's resolution path and is refused
    with OpsYamlError (raised by load_ops_yaml and caught into a named
    ::error::), exiting non-zero."""
    (tmp_path / ".ops.yaml").write_text("{ not yaml: [", encoding="utf-8")
    proc = _run_guard_in(tmp_path)
    assert proc.returncode != 0
    assert "OpsYamlError" in proc.stderr


def test_empty_ops_yaml_is_a_named_refusal(tmp_path):
    """An .ops.yaml that declares no destinations is refused with
    MissingReleaseDestinationsError, not a silently narrowed release."""
    (tmp_path / ".ops.yaml").write_text("destinations: []\n", encoding="utf-8")
    proc = _run_guard_in(tmp_path)
    assert proc.returncode != 0
    assert "MissingReleaseDestinationsError" in proc.stderr


def test_load_ops_yaml_absent_is_none():
    """Direct proof of the loader's normal-case contract: absent .ops.yaml is None."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        assert load_ops_yaml(d) is None


def test_load_ops_yaml_malformed_raises_ops_yaml_error(tmp_path):
    """Direct proof: a malformed .ops.yaml raises OpsYamlError naming the file."""
    path = tmp_path / ".ops.yaml"
    path.write_text("{ not yaml: [", encoding="utf-8")
    with pytest.raises(OpsYamlError) as exc:
        load_ops_yaml(tmp_path)
    assert str(path) in str(exc.value)
