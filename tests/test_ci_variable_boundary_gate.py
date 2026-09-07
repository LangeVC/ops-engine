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

# --- F4: destination/API-host shapes that appear ONLY in a YAML comment. -----
#
# A comment is documentation, never a reach the workflow executes, so a token
# that lives only in a comment must pass the gate. The real hazard is live in
# this tree: mirror.yml:3 documents the mirror as `GitHub (LangeVC/ops-engine)`
# and passes today only because the phrasing avoids the host form; a reword to
# `github.com/LangeVC/ops-engine` used to break the build. The gate now skips
# the comment portion of a line before scanning, matching mirror.yml's own
# resolver, which `continue`s on `#` comment lines.

# The reviewer's F4 shape: a documentation comment naming the mirror repository
# in HOST/OWNER/REPO form, reached by nothing.
COMMENT_DESTINATION = """\
name: Documentation only
on:
  push:
    tags: ["v*"]
jobs:
  x:
    runs-on: ubuntu-latest
    steps:
      - name: A step that reaches no forge repository
        run: echo hi
      # mirror: https://github.com/LangeVC/ops-engine
"""

# The reviewer's F4 api-host shape: an API host named twice, both times only in
# a documentation comment about rate limits.
COMMENT_API_HOST = """\
name: Documentation only
on:
  push:
    tags: ["v*"]
jobs:
  x:
    runs-on: ubuntu-latest
    steps:
      - name: A step that calls no forge API
        run: echo hi
      # api.github.com rate limits are documented at api.github.com
"""

# The scp transport form appearing only in a comment.
COMMENT_SCP = """\
name: Documentation only
on:
  push:
    tags: ["v*"]
jobs:
  x:
    runs-on: ubuntu-latest
    steps:
      - name: A step that pushes over no ssh transport
        run: echo hi
      # git@github.com:LangeVC/ops-engine.git was the pre-ADP-008 ssh transport
"""

# The live hazard, made concrete: mirror.yml's header comment, reworded to name
# the mirror in HOST/OWNER/REPO form. Before comment-awareness this failed the
# build; the workflow reaches nothing the comment names.
COMMENT_MIRROR_HAZARD = """\
name: Mirror to GitHub
# Forgejo (git.langevc.com) is canonical. GitHub (github.com/LangeVC/ops-engine)
# is a read-only mirror. Force-push ONLY the ref that triggered this run.
on:
  push:
    branches: ["**"]
    tags: ["v*"]
jobs:
  x:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""

# --- Do-not-overcorrect: a live destination on a comment-carrying line. -------
#
# Skipping a comment must never hide a real destination: a token inside a
# quoted URL or a ${{ }} expression is executable even when the line also ends
# in a comment, and must still be refused.

REFUSED_HARDCODE_WITH_COMMENT = """\
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
          git push --force "https://x-access-token:${GH_MIRROR_TOKEN}@github.com/LangeVC/ops-engine.git"  # mirror target
"""

REFUSED_VARS_WITH_COMMENT = """\
name: Regression
on:
  push:
    tags: ["v*"]
jobs:
  x:
    runs-on: ubuntu-latest
    steps:
      - name: Render destinations
        run: echo "RELEASE_DESTINATIONS=${{ vars.RELEASE_DESTINATIONS }}"  # regression
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


def _scan_file_with_dest_hosts(path, dest_hosts):
    return subprocess.run(
        [sys.executable, str(GATE), "--dest-hosts", str(dest_hosts), str(path)],
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


# --- Destination-host register: universal shipped, org host externalised. -----
#
# The gate ships the UNIVERSAL destination-host set only (github.com,
# www.github.com, gitlab.com, codeberg.org, git.sr.ht). An organisation's own
# forge host (git.langevc.com) arrives from the config layer via --dest-hosts,
# the same shape --org-vocab/--ci-env take on the src/ side. A self-hosted
# instance must NOT be shipped in the gate.

REMOTE_ON_ORG_FORGE = """\
name: Regression
on:
  push:
    branches: ["**"]
jobs:
  x:
    runs-on: ubuntu-latest
    steps:
      - name: Push to the org forge
        run: |
          git push "https://x-access-token:${TOKEN}@git.langevc.com/LangeVC/ops-engine.git"
"""


def _dest_hosts_file(tmp, hosts, name="dest-hosts.txt"):
    path = Path(tmp) / name
    path.write_text("\n".join(hosts) + "\n", encoding="utf-8")
    return path


def test_org_forge_host_is_not_refused_without_dest_hosts():
    """With no --dest-hosts file the gate refuses only the universal set, so an
    organisation's own forge host is not a shipped refusal — the gate knows no
    organisation. (An org declaring none gets no check for it.)"""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_intmp(tmp, REMOTE_ON_ORG_FORGE, "org-forge.yml")
        r = _scan_file(path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout


def test_org_forge_host_is_refused_when_supplied_via_dest_hosts():
    """The same org forge destination is refused when the host arrives from the
    config layer via --dest-hosts, naming file, line and the destination literal."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_intmp(tmp, REMOTE_ON_ORG_FORGE, "org-forge.yml")
        hosts = _dest_hosts_file(tmp, ["git.langevc.com"])
        r = _scan_file_with_dest_hosts(path, hosts)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "DestinationBoundaryError" in r.stderr
    assert "git.langevc.com/LangeVC/ops-engine.git" in r.stderr


def test_universal_set_is_refused_without_dest_hosts():
    """The universal five (github.com etc.) stay a shipped refusal with no
    --dest-hosts file: the mirror hardcode is refused exactly as before."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_intmp(tmp, RED_PROOF_HARDCODE, "mirror.yml")
        r = _scan_file(path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "github.com/LangeVC/ops-engine.git" in r.stderr


# --- ADP-014: organisation forge host as a live Python value. ------------------
#
# The --py-dir scan extends the same --dest-hosts file to the engine's Python
# source: an organisation forge host supplied via --dest-hosts is refused when it
# appears as a LIVE value in executing Python, and is NOT refused in a docstring
# or comment. The historical case is the FORGEJO_API_DEFAULT constant that named
# the operator's own forge base URL as a fallback. The line is precise: a public
# forge's API host (api.github.com) is legitimately known by the adapter template
# and is not refused here; an organisation's own instance is organisation
# knowledge and must arrive as input.

# The historical live value: a default carrying the operator's own forge host.
ORG_HOST_DEFAULT = '''\
FORGEJO_API_DEFAULT = "https://git.langevc.com/api/v1"
'''


def _write_py_intmp(tmp, text, name="mod.py"):
    path = Path(tmp) / name
    path.write_text(text, encoding="utf-8")
    return path


def _scan_py_dir_with_dest_hosts(py_dir, dest_hosts):
    return subprocess.run(
        [sys.executable, str(GATE), "--py-dir", str(py_dir),
         "--dest-hosts", str(dest_hosts)],
        capture_output=True,
        text=True,
    )


def test_org_forge_host_default_is_refused_as_a_live_value():
    """The historical FORGEJO_API_DEFAULT line — an organisation forge host as a
    live string constant — is refused by the --py-dir scan when the host arrives
    via --dest-hosts, naming file, line and the offending value."""
    with tempfile.TemporaryDirectory() as tmp:
        py_dir = Path(tmp) / "py"
        py_dir.mkdir()
        _write_py_intmp(py_dir, ORG_HOST_DEFAULT, "propose.py")
        hosts = _dest_hosts_file(tmp, ["git.langevc.com"])
        r = _scan_py_dir_with_dest_hosts(py_dir, hosts)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "Layer1BoundaryError" in r.stderr
    assert "git.langevc.com" in r.stderr
    assert ":1:" in r.stderr


def test_org_forge_host_in_a_docstring_is_not_refused():
    """The same organisation forge host appearing only in a docstring is
    documentation and passes; the scan skips the docstring value node."""
    docstring_only = '''\
"""The operator's canonical forge is git.langevc.com; this is documentation."""
x = 1
'''
    with tempfile.TemporaryDirectory() as tmp:
        py_dir = Path(tmp) / "py"
        py_dir.mkdir()
        _write_py_intmp(py_dir, docstring_only, "doc.py")
        hosts = _dest_hosts_file(tmp, ["git.langevc.com"])
        r = _scan_py_dir_with_dest_hosts(py_dir, hosts)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout


def test_public_forge_api_host_is_not_refused_in_python():
    """A public forge's API host (api.github.com) is legitimately known by the
    adapter template and is NOT refused by the --py-dir scan even with a
    --dest-hosts file supplied — the org-host refusal targets only org hosts."""
    public_api = '''\
API_BASE = "https://api.github.com"
UPLOADS_HOST = "https://uploads.github.com"
'''
    with tempfile.TemporaryDirectory() as tmp:
        py_dir = Path(tmp) / "py"
        py_dir.mkdir()
        _write_py_intmp(py_dir, public_api, "adapter.py")
        hosts = _dest_hosts_file(tmp, ["git.langevc.com"])
        r = _scan_py_dir_with_dest_hosts(py_dir, hosts)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout


def test_py_dir_scan_without_dest_hosts_refuses_no_host():
    """With no --dest-hosts file the --py-dir scan refuses no forge host — it
    ships no organisation vocabulary, so the org-host check is nil."""
    with tempfile.TemporaryDirectory() as tmp:
        py_dir = Path(tmp) / "py"
        py_dir.mkdir()
        _write_py_intmp(py_dir, ORG_HOST_DEFAULT, "propose.py")
        r = subprocess.run(
            [sys.executable, str(GATE), "--py-dir", str(py_dir)],
            capture_output=True,
            text=True,
        )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout


def test_whole_src_scripts_tests_pass_py_dir_scan_with_org_hosts_supplied():
    """No public forge host is refused: running the --py-dir scan over src/,
    scripts/ and tests/ with the org host file supplied passes — every live
    public-forge constant (62) and test fixture survives, none is an org host."""
    with tempfile.TemporaryDirectory() as tmp:
        hosts = _dest_hosts_file(tmp, ["git.langevc.com"])
        for rel in ("src", "scripts", "tests"):
            r = subprocess.run(
                [sys.executable, str(GATE), "--py-dir", str(REPO_ROOT / rel),
                 "--dest-hosts", str(hosts)],
                capture_output=True,
                text=True,
            )
            assert r.returncode == 0, (rel, r.stdout + r.stderr)


# --- Tree carve-out: which rule reaches which tree (the ADP-014 rework). -------
#
# The three Layer-1 shapes do not all reach the same tree. Layer 1 is src/ only,
# so the org-name and ci-env-read shapes are enforced on src/ and carved out for
# scripts/ (the operator tools are the calling layer) and tests/. The org-host
# shape is enforced one tree wider — src/ AND scripts/ — because an operator tool
# must receive the forge base URL as input, never default it (ADP-014). A file
# under scripts/ may therefore name the organisation and read a CI variable, but
# may not carry the organisation's forge host as a live value.


def _scan_py_dir(py_dir, org_vocab=None, ci_env=None, dest_hosts=None):
    argv = [sys.executable, str(GATE), "--py-dir", str(py_dir)]
    if org_vocab is not None:
        argv += ["--org-vocab", str(org_vocab)]
    if ci_env is not None:
        argv += ["--ci-env", str(ci_env)]
    if dest_hosts is not None:
        argv += ["--dest-hosts", str(dest_hosts)]
    return subprocess.run(argv, capture_output=True, text=True)


def test_operator_tool_may_name_org_and_read_ci_env():
    """A file under scripts/ is the calling layer, not the engine: it may carry
    an organisation name as a live value and read a CI variable directly, while
    a src/ file naming the same org or reading the same variable is refused."""
    operator_tool = '''\
import os
ORG = "LangeVC"
tok = os.environ.get("GITHUB_TOKEN")
'''
    with tempfile.TemporaryDirectory() as tmp:
        org = _dest_hosts_file(tmp, ["Capacium", "capacium", "capacium-ops",
                                     "LangeVC", "langevc", "lvc-ops"],
                               name="org.txt")
        ci = _dest_hosts_file(tmp, ["GITHUB_REPOSITORY", "GITHUB_TOKEN"],
                              name="ci.txt")
        # scripts/ -> PASS (carve-out)
        scripts_dir = Path(tmp) / "scripts"
        scripts_dir.mkdir()
        _write_py_intmp(scripts_dir, operator_tool, "tool.py")
        r = _scan_py_dir(scripts_dir, org_vocab=org, ci_env=ci)
        assert r.returncode == 0, r.stdout + r.stderr
        # src/ -> FAIL (Layer 1 knows no organisation and no CI system)
        src_dir = Path(tmp) / "src"
        src_dir.mkdir()
        _write_py_intmp(src_dir, operator_tool, "engine.py")
        r = _scan_py_dir(src_dir, org_vocab=org, ci_env=ci)
        assert r.returncode == 1, r.stdout + r.stderr
        assert "Layer1BoundaryError" in r.stderr
        assert "LangeVC" in r.stderr
        assert "GITHUB_TOKEN" in r.stderr


def test_operator_tool_may_not_default_the_org_forge_host():
    """The org-host shape is enforced over scripts/ too: an operator tool that
    names the organisation's forge host as a live value is refused even though
    its org name and CI-variable read pass the carve-out."""
    tool_with_host = '''\
import os
ORG = "LangeVC"
tok = os.environ.get("GITHUB_TOKEN")
API = "https://git.langevc.com/api/v1"
'''
    with tempfile.TemporaryDirectory() as tmp:
        org = _dest_hosts_file(tmp, ["Capacium", "capacium", "capacium-ops",
                                     "LangeVC", "langevc", "lvc-ops"],
                               name="org.txt")
        ci = _dest_hosts_file(tmp, ["GITHUB_REPOSITORY", "GITHUB_TOKEN"],
                              name="ci.txt")
        hosts = _dest_hosts_file(tmp, ["git.langevc.com"])
        scripts_dir = Path(tmp) / "scripts"
        scripts_dir.mkdir()
        _write_py_intmp(scripts_dir, tool_with_host, "tool.py")
        r = _scan_py_dir(scripts_dir, org_vocab=org, ci_env=ci, dest_hosts=hosts)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "Layer1BoundaryError" in r.stderr
    assert "git.langevc.com" in r.stderr


# --- Criterion 4 (F4): comment text is documentation, not a destination. ------


def test_comment_only_destination_mention_passes():
    """A destination-shaped token that appears ONLY in a YAML comment is a
    documentation mention, never a reach the workflow executes, so the gate
    must not refuse it. (Reviewer F4 shape: `# mirror: https://github.com/
    LangeVC/ops-engine`.)"""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_intmp(tmp, COMMENT_DESTINATION)
        r = _scan_file(path)
    assert r.returncode == 0, r.stdout + r.stderr


def test_comment_only_api_host_mention_passes():
    """An API host named only inside a YAML comment (twice, in a note about
    rate limits) is documentation text and must pass. (Reviewer F4 shape.)"""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_intmp(tmp, COMMENT_API_HOST)
        r = _scan_file(path)
    assert r.returncode == 0, r.stdout + r.stderr


def test_comment_only_scp_mention_passes():
    """The scp transport form appearing only in a comment is documentation and
    must pass, exactly like the http(s) and api-host forms."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_intmp(tmp, COMMENT_SCP)
        r = _scan_file(path)
    assert r.returncode == 0, r.stdout + r.stderr


def test_reworded_mirror_header_comment_passes():
    """The live hazard, made concrete: mirror.yml's own header comment names the
    mirror repository only inside a comment. Reworded to the HOST/OWNER/REPO
    form the gate refuses in an executable position, the file must still pass —
    the comment names nothing the workflow reaches."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_intmp(tmp, COMMENT_MIRROR_HAZARD)
        r = _scan_file(path)
    assert r.returncode == 0, r.stdout + r.stderr


def test_destination_in_quotes_on_comment_line_is_still_refused():
    """Do-not-overcorrect: a live destination inside a quoted remote on a line
    that ALSO carries a comment is executable and must still be refused. Only
    the comment portion is skipped."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_intmp(tmp, REFUSED_HARDCODE_WITH_COMMENT)
        r = _scan_file(path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "DestinationBoundaryError" in r.stderr
    assert "github.com/LangeVC/ops-engine.git" in r.stderr


def test_vars_reference_on_comment_line_is_still_refused():
    """Do-not-overcorrect: a vars.* reference inside a ${{ }} expression on a
    line that also carries a comment is executable and must still be refused."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_intmp(tmp, REFUSED_VARS_WITH_COMMENT)
        r = _scan_file(path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "CiVariableBoundaryError" in r.stderr
    assert "vars.RELEASE_DESTINATIONS" in r.stderr


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
    # The org forge host is derived from github.server_url, never a literal: the
    # workflow names no organisation host, and the gate ships none.
    assert "--dest-hosts" in text
    assert "github.server_url" in text
    assert "git.langevc.com" not in text


def test_gate_scans_scripts_for_the_org_host_shape():
    """The ADP-014 defect lived in scripts/mirror-destination-propose.py. The
    wired Layer-1 step must scan scripts/ — not only src/ — so a reintroduced
    forge-host default in an operator tool fails the build. The org-name and
    ci-env shapes stay src/-scoped; only the org-host shape reaches scripts/."""
    text = _release_gate_text()
    assert "--py-dir src" in text
    assert "--py-dir scripts" in text


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


def _bare_host(server_url: str) -> str:
    """The exact POSIX-sh derivation the wired release-gate step performs to
    reduce github.server_url to a bare host: strip the scheme, then any path,
    query or fragment separator, then an optional :port."""
    host = server_url
    if "://" in host:
        host = host.split("://", 1)[1]
    for sep in ("/", "?", "#"):
        host = host.split(sep, 1)[0]
    if ":" in host:
        host = host.split(":", 1)[0]
    return host


def test_bare_host_derivation_reduces_every_server_url_shape():
    """The wired step derives a BARE host, so a port, a trailing slash, a query
    or a fragment all reduce to the plain host a reintroduced org literal
    carries — and an empty/unparseable value reduces to the empty string the
    step refuses by name."""
    assert _bare_host("https://git.langevc.com") == "git.langevc.com"
    assert _bare_host("https://git.langevc.com:9443") == "git.langevc.com"
    assert _bare_host("https://git.langevc.com/") == "git.langevc.com"
    assert _bare_host("https://git.langevc.com?x=1") == "git.langevc.com"
    assert _bare_host("https://git.langevc.com#frag") == "git.langevc.com"
    assert _bare_host("") == ""


def test_release_gate_derives_a_bare_host_not_a_scheme_stripped_remainder():
    """The wired step must strip port/path/query/fragment, not only the scheme:
    `${SERVER_URL#*://}` alone lets the org-host refusal silently vanish, so the
    workflow must reduce to a bare host and refuse an empty result by name."""
    text = _release_gate_text()
    assert "SERVER_HOST=" in text
    assert "${SERVER_URL#*://}" in text
    assert "${SERVER_HOST%%[/?#]*}" in text
    assert "${SERVER_HOST%%:*}" in text
    assert "exit 1" in text


def _derived_org_forge_literal(host: str) -> str:
    return (
        'name: X\non: push\njobs:\n  y:\n    runs-on: ubuntu-latest\n'
        '    steps:\n      - name: a\n'
        '        run: |\n          git push "https://x-access-token:${TOKEN}@'
        + host + '/LangeVC/ops-engine.git"\n'
    )


def test_org_forge_is_refused_across_every_derived_server_url_shape():
    """The org forge literal is refused for a clean AND a ported/pathed/queried
    server_url, because the deduced bare host derives to the plain host the
    reintroduced literal carries. Only the BARE host from _bare_host is fed to
    --dest-hosts, so the org-host refusal never vanishes under a non-plain
    server_url."""
    for server_url in (
        "https://git.langevc.com",
        "https://git.langevc.com:9443",
        "https://git.langevc.com/",
        "https://git.langevc.com?x=1",
    ):
        bare = _bare_host(server_url)
        assert bare == "git.langevc.com", server_url
        with tempfile.TemporaryDirectory() as tmp:
            hosts = _dest_hosts_file(tmp, [bare])
            path = _write_intmp(tmp, _derived_org_forge_literal(bare), "org.yml")
            r = _scan_file_with_dest_hosts(path, hosts)
        assert r.returncode == 1, (server_url, r.stdout + r.stderr)
        assert "DestinationBoundaryError" in r.stderr


def test_empty_server_url_is_refused_not_a_silent_pass():
    """An empty/unparseable server_url derives no bare host; the wired step must
    refuse by name (never run the destination gate with an empty host set, which
    would silently drop the org-host check it exists to run)."""
    assert _bare_host("") == ""
    assert _bare_host("https://") == ""
    # The workflow names the refusal for an empty derived host.
    assert "ServerUrlBoundaryError" in _release_gate_text()


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
    assert "comment text is documentation" in contract
    assert "ci_variable_boundary_gate.py" in contract
    assert "--dest-hosts" in contract
    assert "universal" in contract


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
