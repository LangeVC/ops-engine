"""The release gate reads the E2E verdict from the default branch.

A verdict file in the tag's own tree is the verdict of an earlier release. It
must not shadow the verdict on the default branch that names this candidate.
The step is run as the workflow runs it, in a throwaway repository.
"""
import json
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = [
    ROOT / ".forgejo" / "workflows" / "forgejo-release.yml",
    ROOT / ".forgejo" / "workflows" / "release-gate.yml",
]


def _step_script(workflow: Path) -> str:
    doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name", "").startswith("Require a green E2E verdict"):
                return step["run"]
    raise AssertionError(f"{workflow.name} has no verdict step")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _verdict(sha: str) -> str:
    return json.dumps(
        {"candidate_sha": sha, "result": "green", "runs": [], "produced_at": "2026-11-22T00:00:00Z"}
    )


@pytest.fixture()
def repos(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "t@example.com")
    _git(origin, "config", "user.name", "t")
    (origin / "scripts").mkdir()
    (origin / "scripts" / "e2e_verdict_gate.py").write_text(
        (ROOT / "scripts" / "e2e_verdict_gate.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    # An earlier release left a verdict for another commit in the tree.
    (origin / "e2e-verdict.json").write_text(_verdict("0" * 40), encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "release")
    candidate = _git(origin, "rev-parse", "HEAD")
    _git(origin, "tag", "-a", "v9.9.9", "-m", "v9.9.9")
    # The verdict for this candidate lands on the default branch after it.
    (origin / "e2e-verdict.json").write_text(_verdict(candidate), encoding="utf-8")
    _git(origin, "commit", "-q", "-am", "verdict")
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "checkout", "-q", "v9.9.9")
    return work, candidate


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_the_default_branch_verdict_wins_over_a_stale_one_in_the_tag_tree(repos, workflow):
    work, candidate = repos
    assert "0" * 40 in (work / "e2e-verdict.json").read_text(encoding="utf-8")
    result = subprocess.run(
        ["bash", "-c", _step_script(workflow)],
        cwd=work, capture_output=True, text=True,
        env={
            "PATH": __import__("os").environ["PATH"],
            "HOME": str(work),
            "TAG_NAME": "v9.9.9",
            "VERDICT_PATH": "e2e-verdict.json",
            "DEFAULT_BRANCH": "main",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "read e2e-verdict.json from origin/main" in result.stdout


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_a_missing_verdict_is_still_refused(repos, workflow):
    work, candidate = repos
    origin = Path(_git(work, "remote", "get-url", "origin"))
    _git(origin, "rm", "-q", "e2e-verdict.json")
    _git(origin, "commit", "-q", "-m", "drop the verdict")
    (work / "e2e-verdict.json").unlink()
    result = subprocess.run(
        ["bash", "-c", _step_script(workflow)],
        cwd=work, capture_output=True, text=True,
        env={
            "PATH": __import__("os").environ["PATH"],
            "HOME": str(work),
            "TAG_NAME": "v9.9.9",
            "VERDICT_PATH": "e2e-verdict.json",
            "DEFAULT_BRANCH": "main",
        },
    )
    assert result.returncode != 0
