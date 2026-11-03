"""OE-040: the OSV gate step shows the scanner's own messages and maps its exit codes.

The test executes the ``run:`` block of the step ``Gate the SBOM against OSV``
taken from ``.forgejo/workflows/forgejo-release.yml``; it holds no copy of the
step. The only things replaced are the two things that need the network or a
Linux amd64 host: ``curl`` (which copies a scanner double to the path the step
asks for) and ``sha256sum`` (which accepts). Every other line of the step runs.

Measured exit codes of the pinned osv-scanner 2.5.1
(sha256 f9f25499a2c8cc367b3af45df2ea7eeca7fbccceab9c35079968f4b3652194be for
linux_amd64; the darwin_arm64 build of the same release was run, its SHA-256
checked against the release's osv-scanner_SHA256SUMS), run as
``osv-scanner scan --sbom=<file> --format=json``:

    no findings (idna 3.15)              exit 0    stdout {"results": []}
    findings (django 1.11.0)             exit 1    stdout JSON with results
    unreadable SBOM (not JSON)           exit 127  stdout empty, stderr
                                                   "Error during extraction: ...
                                                   extraction failed on
                                                   specified lockfile"
    SBOM with zero components            exit 128  stdout empty, stderr
                                                   "No package sources found"

So 0 and 1 both mean "the scan ran and wrote its JSON" and the evaluator
decides; any other code is a scanner failure.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".forgejo" / "workflows" / "forgejo-release.yml"
STEP_NAME = "Gate the SBOM against OSV"

EMPTY_RESULT = json.dumps({"results": []})


def _finding_json(score: str) -> str:
    return json.dumps(
        {
            "results": [
                {
                    "packages": [
                        {
                            "package": {"name": "somepkg"},
                            "groups": [
                                {"ids": ["GHSA-test-0001"], "max_severity": score}
                            ],
                        }
                    ]
                }
            ]
        }
    )


def _step_run_block() -> str:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == STEP_NAME:
                return step["run"]
    raise AssertionError(f"step {STEP_NAME!r} not found in {WORKFLOW}")


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def run_step(
    tmp_path: Path, *, stdout: str, stderr: str, exit_code: int
) -> subprocess.CompletedProcess:
    """Run the workflow's own step with a scanner double that behaves as given."""
    work = tmp_path / "work"
    scratch = tmp_path / "scratch"
    bin_dir = tmp_path / "bin"
    for d in (work, scratch, bin_dir):
        d.mkdir()
    (work / "sbom.cdx.json").write_text("{}", encoding="utf-8")

    double = tmp_path / "scanner-double"
    (tmp_path / "double.stdout").write_text(stdout, encoding="utf-8")
    (tmp_path / "double.stderr").write_text(stderr, encoding="utf-8")
    _executable(
        double,
        f'cat "{tmp_path}/double.stdout"\n'
        f'cat "{tmp_path}/double.stderr" >&2\n'
        f"exit {exit_code}\n",
    )
    # curl: copy the double to whatever -o names. sha256sum: accept.
    _executable(
        bin_dir / "curl",
        'while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done\n'
        f'cp "{double}" "$out"\n',
    )
    _executable(bin_dir / "sha256sum", "cat >/dev/null\n")

    # The step writes its scratch files under /tmp; keep them in the test's
    # own directory so runs never collide or leave files behind.
    script = _step_run_block().replace("/tmp/", f"{scratch}/")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
    }
    return subprocess.run(
        ["bash", "-eo", "pipefail", "-c", script],
        check=False,
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def log_of(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout + proc.stderr


def test_scanner_stderr_reaches_the_log_before_the_decision(tmp_path):
    # RED at the base commit: the step discards stderr (2>/dev/null) and exits
    # on the scanner's own status, so this message left no trace.
    proc = run_step(
        tmp_path,
        stdout=_finding_json("5.0"),
        stderr="scanner-double: database unreachable, using cache\n",
        exit_code=1,
    )
    log = log_of(proc)
    assert "scanner-double: database unreachable, using cache" in log


def test_scanner_stdout_reaches_the_log_before_the_decision(tmp_path):
    proc = run_step(
        tmp_path,
        stdout=_finding_json("5.0"),
        stderr="",
        exit_code=1,
    )
    assert "GHSA-test-0001" in log_of(proc)


def test_scanner_messages_come_before_the_decision(tmp_path):
    proc = run_step(
        tmp_path,
        stdout=EMPTY_RESULT,
        stderr="scanner-double: marker-before-decision\n",
        exit_code=0,
    )
    log = log_of(proc)
    assert proc.returncode == 0, log
    assert log.index("marker-before-decision") < log.index("RELEASE PERMITTED")


def test_exit_0_no_findings_permits_the_release(tmp_path):
    proc = run_step(tmp_path, stdout=EMPTY_RESULT, stderr="", exit_code=0)
    assert proc.returncode == 0, log_of(proc)
    assert "RELEASE PERMITTED" in proc.stdout


def test_exit_1_findings_go_to_the_evaluator_not_the_scanner_status(tmp_path):
    # Measured: the scanner exits 1 on any finding. Below the floor the
    # evaluator permits; at or above it refuses. The scanner's 1 decides neither.
    (tmp_path / "below").mkdir()
    below = run_step(
        tmp_path / "below", stdout=_finding_json("6.9"), stderr="", exit_code=1
    )
    assert below.returncode == 0, log_of(below)
    assert "RELEASE PERMITTED" in below.stdout

    (tmp_path / "at").mkdir()
    at = run_step(tmp_path / "at", stdout=_finding_json("7.0"), stderr="", exit_code=1)
    assert at.returncode == 1, log_of(at)
    assert "RELEASE REFUSED" in at.stdout


@pytest.mark.parametrize("code", [127, 128])
def test_other_exit_codes_are_a_scanner_failure_that_names_the_status(tmp_path, code):
    # Measured: 127 = unreadable SBOM, 128 = SBOM with no components.
    proc = run_step(
        tmp_path,
        stdout="",
        stderr="scanner-double: extraction failed\n",
        exit_code=code,
    )
    log = log_of(proc)
    assert proc.returncode != 0
    assert "scanner-double: extraction failed" in log
    assert f"exit {code}" in log
    assert "RELEASE PERMITTED" not in log
