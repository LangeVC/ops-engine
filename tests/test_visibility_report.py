"""P36-004 criteria 1 to 3: a readiness report of refused commits per repository.

Criterion 1: One command prints, per repository, the refused commits with path
and blob SHA and the count refused over all reachable commits, read-only.

Criterion 2: The output can be pasted as exception entries of P36-001 without
editing.

Criterion 3 (RED PROOF): on a repository with a known planning blob the report
lists it; on a clean repository it lists none.

Real git, local repositories: no network. The ``one command`` is exercised as a
subprocess (``python -m ops_engine.modules.visibility_report``), because the
criterion names a command, not a Python import.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from ops_engine.config_loader import OpsEngineConfig

GLOBS = [".skillweave/**", "**/prd*.json"]
CANDIDATE = ".skillweave/prd.json"
SRC = Path(__file__).resolve().parents[1] / "src"


def _run_cli(*args, cwd=None):
    env = dict(os.environ)
    previous = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + previous if previous else "")
    return subprocess.run(
        [sys.executable, "-m", "ops_engine.modules.visibility_report", *args],
        cwd=cwd, capture_output=True, text=True, env=env,
    )


class _World:
    """A local repository with a controlled history."""

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

    def checkout(self, name):
        self._git("checkout", "-q", name)

    def tag(self, name, rev="HEAD"):
        self._git("tag", "-a", name, "-m", name, rev)

    def blob(self, path, rev="HEAD"):
        return self._git("rev-parse", f"{rev}:{path}")

    def status(self):
        return self._git("status", "--porcelain")

    def report(self, *extra):
        args = [str(self.work), "--glob", GLOBS[0], "--glob", GLOBS[1], *extra]
        return _run_cli(*args)


@pytest.fixture
def world(tmp_path):
    w = _World(tmp_path)
    w.commit({"a.txt": "a\n"})
    return w


def _entries(output):
    return [line for line in output.splitlines() if line.strip().startswith("- path:")]


# ─────────────────────────────────────────────────────────────────────────────
# Criterion 3 (RED PROOF) — a repo with a known planning blob lists it;
#                            a clean repo lists none
# ─────────────────────────────────────────────────────────────────────────────

def test_report_lists_a_known_planning_blob(world):
    """RED PROOF: a repository with a known planning blob appears in the report."""
    world.branch("feature-x")
    sha = world.commit({CANDIDATE: "{}\n"})
    blob = world.blob(CANDIDATE)

    result = world.report()

    assert result.returncode == 0, result.stderr
    assert sha in result.stdout
    assert CANDIDATE in result.stdout
    assert blob in result.stdout


def test_report_on_a_clean_repository_lists_none(world):
    """RED PROOF: a repository with no planning blob lists no exception entry."""
    result = world.report()

    assert result.returncode == 0, result.stderr
    assert _entries(result.stdout) == []
    assert "visibility_exceptions:" not in result.stdout
    assert "# commits_refused=0" in result.stdout


# ─────────────────────────────────────────────────────────────────────────────
# Criterion 1 — one command prints per repository the refused commits with
#               path, blob SHA and the count refused
# ─────────────────────────────────────────────────────────────────────────────

def test_one_command_prints_each_refused_commit_with_path_and_blob(world):
    world.branch("feature-x")
    first = world.commit({CANDIDATE: "{}\n"})
    world.commit({"src/main.py": "ok\n"})
    second = world.commit({CANDIDATE: '{"v": 2}\n'})

    result = world.report()

    assert result.returncode == 0, result.stderr
    assert "# repo:" in result.stdout
    assert "# commits_refused=2" in result.stdout
    for commit in (first, second):
        assert f"# commit {commit}" in result.stdout
    assert f"{CANDIDATE} {world.blob(CANDIDATE, first)}" in result.stdout
    assert f"{CANDIDATE} {world.blob(CANDIDATE, second)}" in result.stdout
    # The paste block names each distinct (path, blob) pair once.
    assert len(_entries(result.stdout)) == 2


def test_commits_refused_counts_every_refused_commit_over_all_refs(world):
    """Three offending commits over two distinct blobs: the count is 3.

    The commits are reachable from a branch and from an annotated tag, not from
    HEAD: a HEAD-only walk would report 1 and a (path, blob) pair count 2.
    """
    world.branch("side")
    oldest = world.commit({CANDIDATE: "{}\n"})
    tagged = world.commit({CANDIDATE: '{"v": 2}\n'})
    world.tag("kept", tagged)
    world.checkout("main")
    newest = world.commit({CANDIDATE: "{}\n"})
    world._git("branch", "-D", "side")

    result = world.report()

    assert result.returncode == 0, result.stderr
    assert "# commits_refused=3" in result.stdout
    for commit in (oldest, tagged, newest):
        assert f"# commit {commit}" in result.stdout
    blobs = {world.blob(CANDIDATE, oldest), world.blob(CANDIDATE, tagged)}
    assert len(blobs) == 2
    for blob in blobs:
        assert blob in result.stdout
    # Two distinct pairs, three refused commits.
    assert len(_entries(result.stdout)) == 2


def test_the_report_is_read_only(world):
    world.branch("feature-x")
    world.commit({CANDIDATE: "{}\n"})

    head_before = world._git("rev-parse", "HEAD")
    status_before = world.status()
    world.report()
    assert world._git("rev-parse", "HEAD") == head_before
    assert world.status() == status_before


# ─────────────────────────────────────────────────────────────────────────────
# Criterion 2 — the output pastes as P36-001 exception entries without editing
# ─────────────────────────────────────────────────────────────────────────────

_CONFIG_HEAD = (
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
    f"          - '{GLOBS[0]}'\n"
    f"          - '{GLOBS[1]}'\n"
)


def _paste_into_config(output):
    """Append the report verbatim to the repository's config block."""
    text = _CONFIG_HEAD + output
    raw = yaml.safe_load(text)
    cfg = OpsEngineConfig.load(raw)
    repo_cfg = cfg.get_repo_config("o", "r")
    return repo_cfg, text


def test_output_pastes_unchanged_into_the_visibility_exceptions_block(world):
    world.branch("feature-x")
    world.commit({CANDIDATE: "{}\n"})
    blob = world.blob(CANDIDATE)

    result = world.report("--indent", "8")

    assert result.returncode == 0, result.stderr
    repo_cfg, _text = _paste_into_config(result.stdout)
    assert repo_cfg.visibility_exception_pairs() == ((CANDIDATE, blob),)


def test_a_path_with_yaml_special_characters_also_pastes_unchanged(world):
    awkward = ".skillweave/a: b #c's.json"
    world.branch("feature-x")
    world.commit({awkward: "{}\n"})
    blob = world.blob(awkward)

    result = world.report("--indent", "8")

    assert result.returncode == 0, result.stderr
    repo_cfg, _text = _paste_into_config(result.stdout)
    assert repo_cfg.visibility_exception_pairs() == ((awkward, blob),)


# ─────────────────────────────────────────────────────────────────────────────
# The report never reports a failure as a clean repository
# ─────────────────────────────────────────────────────────────────────────────

def test_a_missing_repository_is_an_error_not_a_clean_report(tmp_path, world):
    result = _run_cli(str(tmp_path / "missing"), "--glob", GLOBS[0])

    assert result.returncode != 0
    assert "visibility_exceptions:" not in result.stdout
    assert "missing" in result.stderr


def test_a_directory_that_is_not_a_git_repository_is_an_error(tmp_path, world):
    plain = tmp_path / "plain"
    plain.mkdir()

    result = _run_cli(str(plain), "--glob", GLOBS[0])

    assert result.returncode != 0
    assert "visibility_exceptions:" not in result.stdout
    assert "not a git repository" in result.stderr
