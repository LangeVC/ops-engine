"""REA-023 — a candidate is released only with a green E2E verdict for its commit.

The verdict is the artifact the release end-to-end harness (REA-022) produces:

    {"candidate_sha": "<40 hex>", "result": "green" | "red",
     "runs": [...], "produced_at": "<string>"}

``candidate_sha`` names the commit the verdict is about. REA-022 has not run
when this lane lands, so the controller fixes the format for this lane; this
file pins it.

What is proven, and how:

* the gate is a script, ``scripts/e2e_verdict_gate.py``, exercised directly
  against every refusal the contract names -- a missing file, malformed JSON, a
  verdict for another SHA, a result that is not green -- and the one
  acceptance, green for the candidate;
* the gate is part of the release workflow, not a manual step: the step
  ``Require a green E2E verdict for the candidate commit`` is taken from
  ``.forgejo/workflows/forgejo-release.yml`` and
  ``.forgejo/workflows/release-gate.yml``, and its own ``run:`` block is
  executed verbatim under ``bash -eo pipefail`` in a throwaway git repository
  carrying the tag. No part of the step is copied here. A red gate leaves no
  ``release-reached.marker``: under the runner's ``set -e`` no later step runs;
* the candidate is the commit the tag PEELS to, not the tag object: an
  annotated tag's object SHA is refused, and the release job of
  ``forgejo-release.yml`` declares ``needs: gate``, so the job holding this
  step must pass before any release object can exist;
* the verdict is evidence ABOUT the candidate and is committed after it, so it
  cannot sit in the candidate's own tree. The step reads it at the repository
  path from the checkout, else from the default branch it was committed to --
  repository content at a repository path, never a secret.

RED at base 2e44cde (the integration tip this lane starts from): neither
workflow carries the step and ``scripts/e2e_verdict_gate.py`` does not exist,
so every test in this file fails. The red proof is the run of this file
against that commit, recorded before the fix.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "e2e_verdict_gate.py"
RELEASE_WORKFLOW = REPO / ".forgejo" / "workflows" / "forgejo-release.yml"
GATE_WORKFLOW = REPO / ".forgejo" / "workflows" / "release-gate.yml"
BOTH = [RELEASE_WORKFLOW, GATE_WORKFLOW]
BOTH_IDS = ["forgejo-release", "release-gate"]

STEP_NAME = "Require a green E2E verdict for the candidate commit"
TAG = "v3.7.0"
CANDIDATE = "1111111111111111111111111111111111111111"
OTHER = "2222222222222222222222222222222222222222"
PRODUCED_AT = "2026-11-20T00:00:00Z"


# --- workflow step extraction ------------------------------------------------

def _jobs(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]


def _job_and_step(path: Path, name: str = STEP_NAME):
    for job_id, job in _jobs(path).items():
        for step in job.get("steps", []):
            if step.get("name") == name:
                return job_id, job, step
    raise AssertionError(f"step {name!r} not found in {path}")


def _declared_verdict_path(step: dict) -> str:
    """The path the step itself declares; it must be repository content."""
    path = (step.get("env") or {}).get("VERDICT_PATH")
    assert path == "e2e-verdict.json", (
        f"the step must read the verdict at the repository path "
        f"'e2e-verdict.json', not {path!r}")
    assert "${{" not in path, "the verdict path is not a runner expression"
    return path


# --- fixtures ----------------------------------------------------------------

def _git(cwd, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _verdict(**over) -> dict:
    payload = {
        "candidate_sha": CANDIDATE,
        "result": "green",
        "runs": [{"run": 1}, {"run": 2}, {"run": 3}],
        "produced_at": PRODUCED_AT,
    }
    payload.update(over)
    return payload


def _write_verdict(work: Path, **over) -> None:
    (work / "e2e-verdict.json").write_text(json.dumps(_verdict(**over)),
                                           encoding="utf-8")


def _candidate_repo(tmp_path: Path) -> Path:
    """A throwaway repository whose annotated tag peels to one commit."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "scripts").mkdir()
    # The checkout a runner would have: the gate script is part of the tree.
    shutil.copy2(SCRIPT, work / "scripts" / "e2e_verdict_gate.py")
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.name", "release-test")
    _git(work, "config", "user.email", "release-test@example.invalid")
    (work / "VERSION").write_text("3.7.0\n", encoding="utf-8")
    _git(work, "add", "VERSION")
    _git(work, "commit", "-q", "-m", "candidate")
    _git(work, "tag", "-a", TAG, "-m", TAG)
    return work


def _candidate(work: Path) -> str:
    return _git(work, "rev-parse", f"refs/tags/{TAG}^{{}}")


def _tag_object(work: Path) -> str:
    return _git(work, "rev-parse", f"refs/tags/{TAG}")


def _run_step(workflow: Path, work: Path) -> subprocess.CompletedProcess:
    """Execute the workflow's own step against the throwaway repository."""
    _, _, step = _job_and_step(workflow)
    verdict_path = _declared_verdict_path(step)
    env = {
        "TAG_NAME": TAG,
        "VERDICT_PATH": verdict_path,
        "DEFAULT_BRANCH": "main",
        "PATH": os.environ["PATH"],
        "HOME": str(work),
        "GIT_TERMINAL_PROMPT": "0",
    }
    return subprocess.run(
        ["bash", "-eo", "pipefail", "-c", step["run"]],
        cwd=work, env=env, capture_output=True, text=True, timeout=120,
        check=False,
    )


def _gate_then_release(workflow: Path, work: Path) -> subprocess.CompletedProcess:
    """The step, then the marker a later release step would leave behind."""
    proc = _run_step(workflow, work)
    if proc.returncode == 0:
        (work / "release-reached.marker").touch()
    return proc


def _log(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout + proc.stderr


# --- criterion 1: no verdict, no release; the message names it ---------------

@pytest.mark.parametrize("workflow", BOTH, ids=BOTH_IDS)
def test_a_candidate_without_a_verdict_is_refused_and_the_message_names_it(
        tmp_path, workflow):
    # RED at base 2e44cde: the step does not exist in either workflow (the
    # AssertionError above) and the gate script does not exist, so nothing
    # refuses a candidate that has no verdict at all.
    work = _candidate_repo(tmp_path)
    proc = _gate_then_release(workflow, work)
    log = _log(proc)

    assert proc.returncode == 1, log
    assert not (work / "release-reached.marker").exists(), (
        "a step after the red gate ran")
    assert "no verdict at 'e2e-verdict.json'" in log, log
    assert "REFUSED" in log, log


# --- criterion 2: a green verdict for another SHA is refused -----------------

@pytest.mark.parametrize("workflow", BOTH, ids=BOTH_IDS)
def test_a_green_verdict_for_another_sha_is_refused_and_names_both(
        tmp_path, workflow):
    work = _candidate_repo(tmp_path)
    candidate = _candidate(work)
    _write_verdict(work, candidate_sha=OTHER)
    proc = _gate_then_release(workflow, work)
    log = _log(proc)

    assert proc.returncode == 1, log
    assert not (work / "release-reached.marker").exists()
    assert OTHER in log and candidate in log, log
    assert "for another SHA" in log, log


@pytest.mark.parametrize("workflow", BOTH, ids=BOTH_IDS)
def test_the_candidate_is_the_peeled_commit_not_the_tag_object(tmp_path, workflow):
    work = _candidate_repo(tmp_path)
    tag_object = _tag_object(work)
    candidate = _candidate(work)
    assert tag_object != candidate, "the tag must be annotated for this proof"
    # A verdict naming the TAG OBJECT is not a verdict about the commit.
    _write_verdict(work, candidate_sha=tag_object)
    proc = _gate_then_release(workflow, work)
    log = _log(proc)

    assert proc.returncode == 1, log
    assert not (work / "release-reached.marker").exists()
    assert tag_object in log and candidate in log, log


# --- the one acceptance: green for the candidate -----------------------------

@pytest.mark.parametrize("workflow", BOTH, ids=BOTH_IDS)
def test_a_green_verdict_for_the_candidate_lets_the_release_step_run(
        tmp_path, workflow):
    work = _candidate_repo(tmp_path)
    candidate = _candidate(work)
    _write_verdict(work, candidate_sha=candidate)
    proc = _gate_then_release(workflow, work)
    log = _log(proc)

    assert proc.returncode == 0, log
    assert (work / "release-reached.marker").exists()
    assert "e2e-verdict-gate: OK" in log, log
    assert candidate in log, log


@pytest.mark.parametrize("workflow", BOTH, ids=BOTH_IDS)
def test_a_red_verdict_for_the_candidate_is_refused(tmp_path, workflow):
    work = _candidate_repo(tmp_path)
    _write_verdict(work, candidate_sha=_candidate(work), result="red")
    proc = _gate_then_release(workflow, work)
    log = _log(proc)

    assert proc.returncode == 1, log
    assert not (work / "release-reached.marker").exists()
    assert "'red'" in log and "not 'green'" in log, log


@pytest.mark.parametrize("workflow", BOTH, ids=BOTH_IDS)
@pytest.mark.parametrize("body, needle", [
    ("{not json", "is not valid JSON"),
    (json.dumps({"result": "green", "runs": [], "produced_at": PRODUCED_AT}),
     "no 'candidate_sha' key"),
    (json.dumps(_verdict(runs="three")), "'runs' is not a list"),
    (json.dumps(_verdict(candidate_sha="deadbeef")),
     "not a 40-character hex commit SHA"),
], ids=["not-json", "missing-key", "runs-type", "short-sha"])
def test_a_malformed_verdict_is_refused(tmp_path, workflow, body, needle):
    work = _candidate_repo(tmp_path)
    (work / "e2e-verdict.json").write_text(body, encoding="utf-8")
    proc = _gate_then_release(workflow, work)
    log = _log(proc)

    assert proc.returncode == 1, log
    assert not (work / "release-reached.marker").exists()
    assert needle in log, log


# --- the verdict is repository content, committed after the candidate --------

@pytest.mark.parametrize("workflow", BOTH, ids=BOTH_IDS)
def test_the_verdict_is_read_from_the_default_branch_when_the_tag_lacks_it(
        tmp_path, workflow):
    # The E2E verdict about commit X is produced after X and committed next to
    # it; the tag's own tree therefore cannot carry it. The step reads it at
    # the repository path from the default branch, which is repository content,
    # not a secret and not a CI variable.
    work = _candidate_repo(tmp_path)
    candidate = _candidate(work)
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(work, "remote", "add", "origin", str(origin))
    _write_verdict(work, candidate_sha=candidate)
    _git(work, "add", "e2e-verdict.json")
    _git(work, "commit", "-q", "-m", "record the verdict for the candidate")
    _git(work, "push", "-q", "origin", "main")
    _git(work, "reset", "-q", "--hard", f"refs/tags/{TAG}^{{}}")
    assert not (work / "e2e-verdict.json").exists(), (
        "the tag tree must not carry the verdict for its own commit")

    proc = _gate_then_release(workflow, work)
    log = _log(proc)

    assert proc.returncode == 0, log
    assert (work / "release-reached.marker").exists()


# --- criterion 3: the gate is part of the release workflow -------------------

def test_the_gate_is_a_step_of_the_job_the_release_job_needs():
    jobs = _jobs(RELEASE_WORKFLOW)
    needed = jobs["release"]["needs"]
    needed = [needed] if isinstance(needed, str) else list(needed)
    assert "gate" in needed, "a red gate could not stop the release job"

    gate_steps = jobs["gate"].get("steps", [])
    assert STEP_NAME in [step.get("name") for step in gate_steps]
    runs = [step.get("run") or "" for step in gate_steps]
    assert any("scripts/e2e_verdict_gate.py" in run for run in runs)


def test_the_tag_time_gate_workflow_carries_the_same_step():
    gate_steps = _jobs(GATE_WORKFLOW)["gate"].get("steps", [])
    assert STEP_NAME in [step.get("name") for step in gate_steps]
    assert any("scripts/e2e_verdict_gate.py" in (step.get("run") or "")
               for step in gate_steps)


@pytest.mark.parametrize("workflow", BOTH, ids=BOTH_IDS)
def test_the_verdict_path_is_repository_content_not_a_secret(workflow):
    _, _, step = _job_and_step(workflow)
    text = json.dumps(step)
    assert "secrets." not in text, "the verdict must not travel as a secret"
    assert "vars." not in text, "the verdict must not travel as a CI variable"
    _declared_verdict_path(step)


# --- the script's own refusal matrix ----------------------------------------

def _run_script(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, check=False,
    )


def test_the_script_refuses_each_cause_with_exit_1(tmp_path):
    good = tmp_path / "e2e-verdict.json"
    good.write_text(json.dumps(_verdict()), encoding="utf-8")

    ok_proc = _run_script("--verdict", str(good), "--candidate-sha", CANDIDATE)
    assert ok_proc.returncode == 0, ok_proc.stdout + ok_proc.stderr
    assert "OK" in ok_proc.stdout

    missing = _run_script("--verdict", str(tmp_path / "absent.json"),
                          "--candidate-sha", CANDIDATE)
    assert missing.returncode == 1
    assert "no verdict at" in missing.stderr

    other = tmp_path / "other.json"
    other.write_text(json.dumps(_verdict(candidate_sha=OTHER)), encoding="utf-8")
    mismatch = _run_script("--verdict", str(other), "--candidate-sha", CANDIDATE)
    assert mismatch.returncode == 1
    assert OTHER in mismatch.stderr and CANDIDATE in mismatch.stderr

    red = tmp_path / "red.json"
    red.write_text(json.dumps(_verdict(result="red")), encoding="utf-8")
    red_proc = _run_script("--verdict", str(red), "--candidate-sha", CANDIDATE)
    assert red_proc.returncode == 1
    assert "not 'green'" in red_proc.stderr

    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    malformed = _run_script("--verdict", str(broken), "--candidate-sha", CANDIDATE)
    assert malformed.returncode == 1
    assert "is not valid JSON" in malformed.stderr


def test_a_usage_error_is_exit_2():
    assert _run_script().returncode == 2
