"""ADP-009 — a forge destination that bypasses the config layer is refused.

This file is the test suite for ``scripts/ci_variable_boundary_gate.py``. It
imports ONLY the standard library and never imports ``ops_engine`` or pytest:
the gate must run on a bare runner that carries neither yaml nor pydantic
(REL-006), so its tests prove the gate through the same constraint. Each
behaviour is proven by a REAL subprocess run of the committed script, not by
reading the script text.

The two red proofs are REAL history, not invented examples:
  * the pre-ADP-004 mirror workflow set `GH_API="https://api.github.com"` and
    `GH_REPO="LangeVC/ops-engine"` as literals and POSTed the GitHub release
    object at `https://api.github.com/repos/...` — the ORIGINAL instance (the
    gate must catch it, not only the later cleans);
  * the pre-ADP-008 release workflow rendered its mirror destinations into the
    `vars.RELEASE_DESTINATIONS` Actions variable and read them back out;
  * the mirror workflow hardcoded `github.com/LangeVC/ops-engine` as its push
    remote.
All looked reasonable and all bypassed the config layer (.ops.yaml). The
faithful excerpts below are copied from `git show de302bc`,
`git show a96cab7~2` and `git show a96cab7` respectively; the test replaces
them with nothing and asserts the gate refuses each on its exact bytes.

The file runs two ways:
  * ``python3 tests/test_ci_variable_boundary_gate.py`` — standalone, no pytest
    needed (the ``__main__`` harness below);
  * pytest collection — the same ``test_*`` functions are plain functions with
    plain asserts, so no non-stdlib import is required either way.
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "scripts" / "ci_variable_boundary_gate.py"
WORKFLOW_DIR = REPO_ROOT / ".forgejo" / "workflows"

GATE_CONTENT = GATE.read_text(encoding="utf-8")

# --- REAL red-proof history bytes (see docstring). --------------------------

# From git show de302bc:.forgejo/workflows/forgejo-release.yml (ADP-004 shape, the
# ORIGINAL instance this gate exists to stop recurring). The forge identity rode
# as two literals — an API host and the OWNER/REPO it was concatenated into.
RED_PROOF_API_LITERALS = """\
      - name: Create GitHub release
        env:
          GH_MIRROR_TOKEN: ${{ secrets.GH_MIRROR_TOKEN }}
        run: |
          set -euo pipefail

          TAG_NAME="${{ github.event.inputs.tag_name || github.ref_name }}"
          GH_API="https://api.github.com"
          GH_REPO="LangeVC/ops-engine"

          GH_REL="$(curl -fsSL -X POST "${GH_API}/repos/${GH_REPO}/releases" \\
            -H "Authorization: token ${GH_MIRROR_TOKEN}")" || {
            echo "GitHubReleaseError" >&2
            exit 1
          }
      - name: Upload assets to GitHub
        run: |
          GH_REPO="LangeVC/ops-engine"
          curl -fsSL -X POST \\
            "https://uploads.github.com/repos/${GH_REPO}/releases/1/assets" \\
            -H "Authorization: token ${GH_MIRROR_TOKEN}" || exit 1
"""

# From git show a96cab7~2:.forgejo/workflows/forgejo-release.yml (ADP-004 shape).
# The mirror destination was rendered into a repo/org-level Actions variable and
# read back out of it later in the same step.
RED_PROOF_VARS = """\
      - name: Create Release
        env:
          TAG_NAME: "${{ github.event.inputs.tag_name || github.ref_name }}"
          RELEASE_DESTINATIONS: "${{ vars.RELEASE_DESTINATIONS }}"
        run: |
          set -euo pipefail
"""

# From git show a96cab7:.forgejo/workflows/mirror.yml. The mirror push remote
# hardcoded the destination repository by value rather than reading .ops.yaml.
RED_PROOF_HARDCODE = """\
      - name: Push to GitHub mirror
        env:
          GH_MIRROR_TOKEN: ${{ secrets.GH_MIRROR_TOKEN }}
        run: |
          remote="https://x-access-token:${GH_MIRROR_TOKEN}@github.com/LangeVC/ops-engine.git"
          git push --force "$remote" "+${GITHUB_REF}:${GITHUB_REF}"
"""

# --- Probes read back into the gate (criterion READS what the script wrote). --

# A permitted workflow using a SECRET as a credential and Forgejo-provided event
# context as the run identity. None of these is a destination and none may fire.
PERMITTED_SECRET_EVENT = """\
name: Clean runner
on:
  push:
    tags: ["v*"]
jobs:
  x:
    runs-on: ubuntu-latest
    steps:
      - name: Push to the canonical forge with the event token
        env:
          TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          git push "https://x-access-token:${TOKEN}@${GITHUB_SERVER_URL#https://}/${GITHUB_REPOSITORY}.git"
"""

# A permitted workflow that downloads a whitelisted TOOL SUPPLIER (not a
# destination) and names its OWN repository only through event context.
PERMITTED_TOOL_SUPPLIER = """\
name: Fetch tooling
on:
  push:
    tags: ["v*"]
jobs:
  x:
    runs-on: ubuntu-latest
    steps:
      - uses: https://github.com/actions/checkout@v4
      - name: Download the OSV scanner
        run: |
          curl -L "https://github.com/google/osv-scanner/releases/download/v1.0/scanner"
"""

# The gate must refuse each REFUSED form by value.
REFUSED_VARS = """\
name: Regression
on:
  push:
    tags: ["v*"]
jobs:
  x:
    runs-on: ubuntu-latest
    steps:
      - name: Render destinations
        run: echo "RELEASE_DESTINATIONS=${{ vars.RELEASE_DESTINATIONS }}"
"""

REFUSED_HARDCODE = """\
name: Regression
on:
  push:
    branches: ["**"]
jobs:
  x:
    runs-on: ubuntu-latest
    steps:
      - name: Push to the mirror
        env:
          GH_MIRROR_TOKEN: ${{ secrets.GH_MIRROR_TOKEN }}
        run: |
          git push --force "https://x-access-token:${GH_MIRROR_TOKEN}@github.com/LangeVC/ops-engine.git" "+${GITHUB_REF}:${GITHUB_REF}"
"""

# The scp-form git remote. Same OWNER/REPO destination by value, only a
# different transport prefix (`git@` + `:` after the host); the contract names
# this form and the gate must refuse it too.
REFUSED_SCP = """\
name: Regression
on:
  push:
    branches: ["**"]
jobs:
  x:
    runs-on: ubuntu-latest
    steps:
      - name: Push to the mirror over ssh
        run: |
          git push "git@github.com:LangeVC/ops-engine.git" "+${GITHUB_REF}:${GITHUB_REF}"
"""

# A forge API URL that names the target by value (the other prose in CONTRACT.md).
REFUSED_API_URL = """\
name: Regression
on:
  push:
    tags: ["v*"]
jobs:
  x:
    runs-on: ubuntu-latest
    steps:
      - name: Create the GitHub release object
        run: |
          curl -X POST "https://api.github.com/repos/LangeVC/ops-engine/releases"
"""


def _write_intmp(tmp, text, name="workflow.yml"):
    path = Path(tmp) / name
    path.write_text(text, encoding="utf-8")
    return path


def _scan_file(path):
    return subprocess.run(
        [sys.executable, str(GATE), str(path)],
        capture_output=True,
        text=True,
    )


# --- Criterion 1: both REAL red proofs refuse, naming file/line/token --------


def test_refuses_the_pre_adp008_vars_destination():
    """The pre-ADP-008 release workflow rendered destinations into
    vars.RELEASE_DESTINATIONS. The gate refuses that exact historical shape,
    naming the offending token and its line."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_intmp(tmp, RED_PROOF_VARS, "release.yml")
        r = _scan_file(path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "release.yml" in r.stderr
    assert "vars.RELEASE_DESTINATIONS" in r.stderr
    # RELEASE_DESTINATIONS sits on line 4 of the excerpt.
    assert ":4:" in r.stderr
    assert "CiVariableBoundaryError" in r.stderr


def test_refuses_the_original_adp004_api_literals():
    """The ORIGINAL instance — the pre-ADP-004 release workflow that set
    GH_API=https://api.github.com and concatenated GH_REPO into the request.
    Neither line carries an OWNER/REPO of its own, so only refusing the forge
    API host by name closes this shape. The gate must refuse it (references the
    API host and upload host by value)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_intmp(tmp, RED_PROOF_API_LITERALS, "adp004.yml")
        r = _scan_file(path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "adp004.yml" in r.stderr
    assert "api.github.com" in r.stderr
    assert "uploads.github.com" in r.stderr
    assert "DestinationBoundaryError" in r.stderr


def test_refuses_the_historical_hardcoded_mirror_destination():
    """Today's mirror.yml hardcoded github.com/LangeVC/ops-engine as its push
    remote. The gate refuses that exact historical shape, naming file, line and
    the offending destination literal."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_intmp(tmp, RED_PROOF_HARDCODE, "mirror.yml")
        r = _scan_file(path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "mirror.yml" in r.stderr
    assert "github.com/LangeVC/ops-engine.git" in r.stderr
    assert "5" in r.stderr
    assert "DestinationBoundaryError" in r.stderr


def test_refuses_a_resurrected_mirror(_=None):
    """Same as the historical mirror: a hardcoded destination reappearing in a
    copy of the mirror workflow fails the gate no matter the file name."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_intmp(tmp, RED_PROOF_HARDCODE, "mirror-copy.yml")
        r = _scan_file(path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "DestinationBoundaryError" in r.stderr


# --- Criterion 2: permitted forms pass, refused forms fail -------------------


def test_secrets_and_event_context_are_permitted():
    """secrets.* is a credential, not a destination, and github.* is
    Forgejo-provided event context, not a user variable store. A workflow that
    uses only those passes the gate."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_intmp(tmp, PERMITTED_SECRET_EVENT)
        r = _scan_file(path)
    assert r.returncode == 0, r.stdout + r.stderr


def test_tool_suppliers_are_permitted():
    """The gate refuses destinations, not tool downloads: actions/checkout and
    the OSV scanner are whitelisted suppliers and a workflow naming them
    passes."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_intmp(tmp, PERMITTED_TOOL_SUPPLIER)
        r = _scan_file(path)
    assert r.returncode == 0, r.stdout + r.stderr


def test_user_defined_variable_is_refused():
    """A workflow that carries a destination in a vars.* variable passes
    nothing and is refused."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_intmp(tmp, REFUSED_VARS)
        r = _scan_file(path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "vars.RELEASE_DESTINATIONS" in r.stderr


def test_hardcoded_destination_is_refused():
    """A workflow that names a forge repository by value passes nothing and is
    refused."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_intmp(tmp, REFUSED_HARDCODE)
        r = _scan_file(path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "github.com/LangeVC/ops-engine.git" in r.stderr


def test_scp_form_destination_is_refused():
    """The scp git remote `git@host:owner/repo` names the same destination by
    value through a different transport; the contract names it and the gate
    refuses it."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_intmp(tmp, REFUSED_SCP)
        r = _scan_file(path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "DestinationBoundaryError" in r.stderr
    assert "git@github.com:LangeVC/ops-engine.git" in r.stderr


def test_forge_api_url_naming_the_target_is_refused():
    """A forge API URL that names the target by value (api.github.com/repos/
    OWNER/REPO/...) is refused at the API host: the register the contract names
    for a destination must not be named by value."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_intmp(tmp, REFUSED_API_URL)
        r = _scan_file(path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "DestinationBoundaryError" in r.stderr
    assert "api.github.com" in r.stderr


# --- Criterion 3: mirror reads config, whole tree passes, wired in CI --------


def test_mirror_reads_its_destination_from_ops_yaml():
    """mirror.yml must not name a destination literal. It must read the github
    destination out of the committed .ops.yaml; the credential stays a secret."""
    text = (WORKFLOW_DIR / "mirror.yml").read_text(encoding="utf-8")
    # It reads .ops.yaml and rejects a missing github destination by name.
    assert ".ops.yaml" in text
    assert "MirrorDestinationBoundaryError" in text
    # Its only push remote hosts the destination value the config layer supplied,
    # not a hardcoded owner/repo.
    assert "github.com/LangeVC/ops-engine" not in text
    assert "${mirror_repo}" in text


def _mirror_destination_resolver() -> str:
    """The stdlib python mirror.yml invokes to resolve the github destination.

    mirror.yml is a YAML block-scalar whose lines carry the workflow's
    indentation in the raw file; CI decodes it to column-0 python. Dedent the
    heredoc body the same way before running it.
    """
    lines = (WORKFLOW_DIR / "mirror.yml").read_text(encoding="utf-8").splitlines()
    start = next(
        i for i, ln in enumerate(lines) if "python3 - <<'PY'" in ln
    )
    body = []
    for ln in lines[start + 1:]:
        if ln.strip() == "PY":
            break
        body.append(ln)
    pad = min((len(ln) - len(ln.lstrip(" ")) for ln in body if ln.strip()))
    return "\n".join(ln[pad:] if ln.strip() else ln for ln in body)


def test_mirror_destination_resolver_reads_the_real_ops_yaml():
    """Run the exact resolver mirror.yml runs against the committed .ops.yaml: it
    must echo the sole github destination repo (the config-layer value), and a
    copy whose destinations drop github must refuse instead of guessing."""
    resolver = _mirror_destination_resolver()
    with tempfile.TemporaryDirectory() as tmp:
        ops = Path(tmp) / ".ops.yaml"
        ops.write_text(
            (REPO_ROOT / ".ops.yaml").read_text(encoding="utf-8"), encoding="utf-8"
        )
        r = subprocess.run(
            [sys.executable, "-c", resolver], cwd=tmp, capture_output=True, text=True
        )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "LangeVC/ops-engine"

    # Drop the whole github destination entry (the mirror must refuse, never
    # guess a repo): the github block is the final entry, so cut it and all its
    # sibling value lines to the end of the file.
    full = (REPO_ROOT / ".ops.yaml").read_text(encoding="utf-8").splitlines()
    start = next(
        i for i, line in enumerate(full) if line.strip() == "- forge: github"
    )
    reduced = "\n".join(full[:start]) + "\n"
    with tempfile.TemporaryDirectory() as tmp:
        ops = Path(tmp) / ".ops.yaml"
        ops.write_text(reduced, encoding="utf-8")
        r = subprocess.run(
            [sys.executable, "-c", resolver], cwd=tmp, capture_output=True, text=True
        )
    assert r.returncode == 2, r.stdout + r.stderr
    assert "exactly one github destination" in r.stderr


def _ops_yaml_with(destinations_body: str) -> str:
    """A minimal committed .ops.yaml carrying only the given github destination
    block, mirroring the real-but-minimised shape the resolver reads."""
    return (
        "config_version: 1\n"
        "destinations:\n"
        + destinations_body
        + "\n"
    )


def test_mirror_destination_resolver_strips_yaml_quoting():
    """Quoting the repo value (which load_ops_yaml ACCEPTS and unquotes) must not
    leak into the push remote: the resolver strips the quotes and emits the bare
    repo — matching the real loader's resolution, not the raw bytes."""
    resolver = _mirror_destination_resolver()
    body = (
        "  - forge: forgejo\n"
        "    repo: langevc/ops-engine\n"
        "    role: release\n"
        "  - forge: github\n"
        '    repo: "LangeVC/ops-engine"\n'
        "    role: release\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        ops = Path(tmp) / ".ops.yaml"
        ops.write_text(_ops_yaml_with(body), encoding="utf-8")
        r = subprocess.run(
            [sys.executable, "-c", resolver], cwd=tmp, capture_output=True, text=True
        )
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip() == "LangeVC/ops-engine"


def test_mirror_destination_resolver_never_guesses_an_inline_comment():
    """An inline comment after the repo value (which load_ops_yaml ACCEPTS and
    strips) must make the resolver REFUSE by name, never emit the comment text
    into a push remote. The refusal is deliberate, not a downstream git error."""
    resolver = _mirror_destination_resolver()
    body = (
        "  - forge: forgejo\n"
        "    repo: langevc/ops-engine\n"
        "    role: release\n"
        "  - forge: github\n"
        "    repo: LangeVC/ops-engine  # the mirror target\n"
        "    role: release\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        ops = Path(tmp) / ".ops.yaml"
        ops.write_text(_ops_yaml_with(body), encoding="utf-8")
        r = subprocess.run(
            [sys.executable, "-c", resolver], cwd=tmp, capture_output=True, text=True
        )
    assert r.returncode == 2, r.stdout + r.stderr
    assert "MirrorDestinationBoundaryError" in r.stderr
    assert "inline comment" in r.stderr


def test_whole_tree_passes_the_gate():
    """Every workflow under .forgejo today passes the boundary gate."""
    r = subprocess.run(
        [sys.executable, str(GATE), "--dir", str(WORKFLOW_DIR)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout


def _release_gate_text() -> str:
    return (WORKFLOW_DIR / "release-gate.yml").read_text(encoding="utf-8")


def test_gate_is_wired_to_fail_the_build_in_ci():
    """release-gate.yml runs the committed boundary gate over .forgejo before
    the version gate, so a reintroduced destination fails the build at tag time,
    before the release step can publish anywhere."""
    text = _release_gate_text()
    assert "ci_variable_boundary_gate.py --dir .forgejo" in text


def test_deliberately_reintroduced_destination_fails_the_wired_build():
    """Exactly what the wired release-gate step runs fails the build when a
    destination is reintroduced: reintroduce one in a mirror copy and run the
    committed script the way the CI step does; it exits non-zero (step red)."""
    r = subprocess.run(
        [sys.executable, str(GATE), "--dir", str(WORKFLOW_DIR)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr

    with tempfile.TemporaryDirectory() as tmp:
        dirpath = Path(tmp) / "workflows"
        dirpath.mkdir(parents=True)
        (dirpath / "mirror-reintroduced.yml").write_text(
            RED_PROOF_HARDCODE, encoding="utf-8"
        )
        reintro = subprocess.run(
            [sys.executable, str(GATE), "--dir", str(dirpath)],
            capture_output=True,
            text=True,
        )
    assert reintro.returncode == 1, reintro.stdout + reintro.stderr
    assert "DestinationBoundaryError" in reintro.stderr
    assert r.returncode == 0, r.stdout + r.stderr


def test_gate_runs_without_site_packages():
    """The script executes under `python3 -S`: no site-packages module (yaml,
    pydantic, ops_engine) may be imported, exactly the bare-runner constraint
    REL-006 established."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_intmp(tmp, PERMITTED_SECRET_EVENT)
        r = subprocess.run(
            [sys.executable, "-S", str(GATE), str(path)],
            capture_output=True,
            text=True,
        )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "import ops_engine" not in GATE_CONTENT
    assert "ops_engine import" not in GATE_CONTENT
    assert re.search(r"^import\s+ops_engine\b", GATE_CONTENT, re.MULTILINE) is None


def test_contract_documents_the_permitted_set():
    """CONTRACT.md defends the boundary: secrets are not destinations and
    Forgejo-provided event context is not a user variable store, while vars.*
    and hardcoded destination literals are refused."""
    contract = (REPO_ROOT / "CONTRACT.md").read_text(encoding="utf-8")
    assert "## Destination boundary (ADP-009)" in contract
    assert "vars.<NAME>" in contract
    assert "Secrets are not destinations" in contract
    assert "Forgejo-provided event context is not a user variable store" in contract
    assert "tool-fetch suppliers" in contract
    assert "ci_variable_boundary_gate.py" in contract


def _main() -> None:
    failures = []
    ran = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            ran += 1
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - report and continue
                failures.append(name)
                sys.stdout.write("FAIL %s: %r\n" % (name, exc))
            else:
                sys.stdout.write("PASS %s\n" % name)
    sys.stdout.write("\n%d run, %d failed\n" % (ran, len(failures)))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    _main()
