"""P36-004 criteria 1 to 3: a readiness report of refused commits per repository.

Criterion 1: One command prints, per repository, the refused commits with path
and blob SHA and the count refused over all reachable commits, read-only.

Criterion 2: The output can be pasted as exception entries of P36-001 without
editing.

Criterion 3 (RED PROOF): on a repository with a known planning blob the report
lists it; on a clean repository it lists none.

Real git, local bare repositories, forge URLs rewritten to ``file://``: no
network.
"""

import subprocess

import pytest

from ops_engine.config_loader import OpsEngineConfig

GLOBS = [".skillweave/**", "**/prd*.json"]
CANDIDATE = ".skillweave/prd.json"


class _World:
    """A work repo plus a local bare canonical repository."""

    def __init__(self, tmp_path):
        self.work = tmp_path / "work"
        self.work.mkdir()
        self._git("init", "-q", "-b", "main")

    def _git(self, *args, cwd=None):
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            cwd=cwd or self.work, check=True, capture_output=True, text=True,
        ).stdout.strip()

    def commit(self, files):
        for name, text in files.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self._git("add", ".")
        self._git("commit", "-q", "-m", "c")
        return self._git("rev-parse", "HEAD")

    def branch(self, name, start="HEAD"):
        self._git("checkout", "-q", "-b", name, start)

    def blob(self, path, rev="HEAD"):
        return self._git("rev-parse", f"{rev}:{path}")


@pytest.fixture
def world(tmp_path):
    w = _World(tmp_path)
    w.commit({"a.txt": "a\n"})
    return w


# ─────────────────────────────────────────────────────────────────────────────
# Criterion 3 (RED PROOF) — a repo with a known planning blob lists it;
#                            a clean repo lists none
# ─────────────────────────────────────────────────────────────────────────────

def test_report_lists_a_known_planning_blob(world):
    """RED PROOF: a repository with a known planning blob appears in the report."""
    world.branch("feature-x")
    sha = world.commit({CANDIDATE: "{}\n"})
    blob = world.blob(CANDIDATE)

    from ops_engine.modules.visibility_report import generate_report
    output = generate_report(str(world.work), globs=GLOBS)

    assert sha in output
    assert CANDIDATE in output
    assert blob in output


def test_report_on_a_clean_repository_lists_none(world):
    """RED PROOF: a repository with no planning blob lists nothing."""
    from ops_engine.modules.visibility_report import generate_report
    output = generate_report(str(world.work), globs=GLOBS)

    assert output == "" or output.strip() == ""


# ─────────────────────────────────────────────────────────────────────────────
# Criterion 1 — one command prints per-repository refused commits with
#               path, blob SHA and the count refused
# ─────────────────────────────────────────────────────────────────────────────

def test_report_includes_count_refused(world):
    world.branch("feature-x")
    world.commit({CANDIDATE: "{}\n"})
    world.commit({"src/main.py": "ok\n"})
    world.commit({CANDIDATE: '{"v": 2}\n'})

    from ops_engine.modules.visibility_report import generate_report
    output = generate_report(str(world.work), globs=GLOBS)

    assert "commits_refused=" in output
    # At least 2 commits carry candidate content on this branch
    lines = [l for l in output.splitlines() if "commits_refused=" in l]
    assert lines
    count = int(lines[0].split("commits_refused=")[1])
    assert count >= 2


def test_report_is_read_only(world):
    """The report must not modify the repository (read-only)."""
    world.branch("feature-x")
    world.commit({CANDIDATE: "{}\n"})

    from ops_engine.modules.visibility_report import generate_report
    head_before = world._git("rev-parse", "HEAD")
    generate_report(str(world.work), globs=GLOBS)
    head_after = world._git("rev-parse", "HEAD")

    assert head_before == head_after


# ─────────────────────────────────────────────────────────────────────────────
# Criterion 2 — output can be pasted as P36-001 exception entries
# ─────────────────────────────────────────────────────────────────────────────

def test_output_is_pasteable_as_exception_entries(world):
    """Each refused commit line must be pasteable as a visibility_exception entry."""
    world.branch("feature-x")
    sha = world.commit({CANDIDATE: "{}\n"})
    blob = world.blob(CANDIDATE)

    from ops_engine.modules.visibility_report import generate_report
    output = generate_report(str(world.work), globs=GLOBS)

    # The output should contain lines like:
    #   - path: .skillweave/prd.json
    #     blob: <sha>
    # which is directly pasteable into config.yml visibility_exceptions
    assert f"path: {CANDIDATE}" in output
    assert f"blob: {blob}" in output


def test_exception_entries_load_into_config(world):
    """The output entries parse as valid VisibilityException configuration."""
    world.branch("feature-x")
    sha = world.commit({CANDIDATE: "{}\n"})
    blob = world.blob(CANDIDATE)

    from ops_engine.modules.visibility_report import generate_report
    output = generate_report(str(world.work), globs=GLOBS)

    # Build a YAML fragment from the output and verify it loads.
    # The report outputs list items with proper YAML indentation:
    #   - path: <path>
    #     blob: <sha>
    # We need blob to be indented 2 more than the dash line.
    yaml_fragment = (
        "orgs:\n"
        "  o:\n"
        "    repositories:\n"
        "      r:\n"
        "        destinations:\n"
        "          - forge: github\n"
        "            repo: o/r\n"
        "            role: mirror\n"
        "            visibility: public\n"
        "        visibility_candidate_globs:\n"
        f'          - "{GLOBS[0]}"\n'
        f'          - "{GLOBS[1]}"\n'
        "        visibility_exceptions:\n"
    )
    # Extract pasteable lines from the output.
    # The report lines use 2-space indent for the list dash and 4-space for blob.
    # Convert to the config's 10-space base + 2/4 offset.
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("- path:"):
            yaml_fragment += f"          {stripped}\n"
        elif stripped.startswith("blob:"):
            yaml_fragment += f"            {stripped}\n"

    import yaml
    raw = yaml.safe_load(yaml_fragment)
    cfg = OpsEngineConfig.load(raw)
    repo_cfg = cfg.get_repo_config("o", "r")
    pairs = repo_cfg.visibility_exception_pairs()
    assert len(pairs) >= 1
    assert any(p == (CANDIDATE, blob) for p in pairs)
