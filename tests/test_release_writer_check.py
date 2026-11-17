"""release-writer-check: no workflow of either forge writes a release object
outside the engine path.

The property under test (RFL-021 criterion 1) is: a release object is created,
edited, or deleted by exactly one code path, ``ReleaseHandler.publish_release``
called from ``.forgejo/workflows/forgejo-release.yml``. Every other workflow --
Forgejo ``.forgejo/workflows/`` and GitHub ``.github/workflows/`` alike -- must
only read a release or describe one in a comment.

Two shapes are proven here:

- **the real tree is green** -- the check exits 0 against this repository's
  own two-forge workflow tree, and names the one sanctioned engine call;
- **a synthetic writer is red** -- a throwaway workflow file that creates a
  release object without the engine makes the check exit 1 and name the file
  and line. That synthetic file is the red proof: the check is shown to
  discriminate, not merely to print OK. Both forge roots are covered by a
  negative control, and read-only shapes (a ``/releases/download/`` fetch, a
  comment naming a write) stay green so the census cannot be passed by
  matching every occurrence of the word "release".

Base parity is proven by the same check being run against the base tree: at
``01ca1cc1`` there was likewise exactly one writer, the engine call. The check
is therefore green at the base and at the tip -- it is a durable guard, not a
one-shot fix. A red probe against the base is impossible by construction
because the base carries no second writer; the criterion's red proof is the
negative control below.

All fixtures are written into pytest's ``tmp_path``; no test writes into this
repository.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "release-writer-check.py"

spec = importlib.util.spec_from_file_location("release_writer_check", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def run_check(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _tree(tmp_path: Path, relative: str, body: str) -> Path:
    """Write one workflow file at ``relative`` under ``tmp_path``."""
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# --- the real tree is green -------------------------------------------------

def test_real_two_forge_tree_has_no_writer_outside_the_engine():
    result = run_check("--root", str(REPO))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no release-object writer outside the engine path" in result.stdout
    # The one sanctioned writer is still present and still named as the
    # engine call -- removing it would be a different defect, not a pass.
    assert "engine call publish_release (sanctioned)" in result.stdout
    assert "forgejo-release.yml" in result.stdout


def test_both_forges_are_scanned():
    """Neither workflow root may be skipped silently."""
    result = run_check("--root", str(REPO))
    assert result.returncode == 0, result.stdout + result.stderr
    forgejo = list((REPO / ".forgejo/workflows").glob("*.yml"))
    github = list((REPO / ".github/workflows").glob("*.yml"))
    assert forgejo and github, "both forge workflow roots must carry a workflow"
    assert f"scanned {len(forgejo) + len(github)} workflow file(s)" in result.stdout


# --- negative controls: a synthetic writer turns the check red --------------

def test_synthetic_gh_release_create_is_red(tmp_path: Path):
    _tree(tmp_path, ".forgejo/workflows/release.yml", (
        "jobs:\n"
        "  release:\n"
        "    steps:\n"
        "      - run: gh release create \"$TAG\" dist/*.whl\n"
    ))
    result = run_check("--root", str(tmp_path))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "gh release create" in result.stdout
    assert ".forgejo/workflows/release.yml:4" in result.stdout


def test_synthetic_http_post_to_releases_is_red(tmp_path: Path):
    """A curl POST wrapped over lines, with the endpoint two lines down."""
    _tree(tmp_path, ".forgejo/workflows/release.yml", (
        "jobs:\n"
        "  r:\n"
        "    steps:\n"
        "      - run: |\n"
        "          curl -sSf -X POST \\\n"
        "            -H \"Authorization: token $TOKEN\" \\\n"
        "            \"https://example.com/api/v1/repos/o/r/releases\"\n"
    ))
    result = run_check("--root", str(tmp_path))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "/releases endpoint" in result.stdout


def test_synthetic_github_action_writer_is_red(tmp_path: Path):
    """The GitHub forge root is covered by its own negative control."""
    _tree(tmp_path, ".github/workflows/release.yml", (
        "jobs:\n"
        "  r:\n"
        "    steps:\n"
        "      - uses: softprops/action-gh-release@v2\n"
    ))
    result = run_check("--root", str(tmp_path))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "action-gh-release" in result.stdout


def test_synthetic_python_writer_is_red(tmp_path: Path):
    _tree(tmp_path, ".github/workflows/release.yml", (
        "jobs:\n"
        "  r:\n"
        "    steps:\n"
        "      - run: |\n"
        "          python3 - <<'PY'\n"
        "          import requests\n"
        "          requests.post(f\"{base}/api/v1/repos/o/r/releases\", json=body)\n"
        "          PY\n"
    ))
    result = run_check("--root", str(tmp_path))
    assert result.returncode == 1, result.stdout + result.stderr


# --- read-only shapes stay green -------------------------------------------

def test_release_download_is_a_read_not_a_writer(tmp_path: Path):
    _tree(tmp_path, ".forgejo/workflows/release.yml", (
        "jobs:\n"
        "  r:\n"
        "    steps:\n"
        "      - run: curl -sSLo osv "
        "https://github.com/google/osv-scanner/releases/download/v2.0.0/osv\n"
    ))
    result = run_check("--root", str(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr


def test_comment_naming_a_write_is_not_a_writer(tmp_path: Path):
    _tree(tmp_path, ".forgejo/workflows/release.yml", (
        "jobs:\n"
        "  r:\n"
        "    steps:\n"
        "      # gh release create is what the engine replaced\n"
        "      - run: echo ok\n"
    ))
    result = run_check("--root", str(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr


def test_engine_call_is_reported_not_refused(tmp_path: Path):
    _tree(tmp_path, ".forgejo/workflows/release.yml", (
        "jobs:\n"
        "  r:\n"
        "    steps:\n"
        "      - run: |\n"
        "          report = await ReleaseHandler.publish_release(\n"
        "              config, repo, tag_name)\n"
    ))
    result = run_check("--root", str(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "engine call publish_release (sanctioned)" in result.stdout


# --- named refusals --------------------------------------------------------

def test_nothing_to_scan_is_a_named_refusal(tmp_path: Path):
    result = run_check("--root", str(tmp_path))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "no workflow files found" in result.stderr
    assert "Traceback" not in result.stderr
