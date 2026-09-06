"""REL-011 — the release description is addressed at an external reader.

This file is the test suite for ``scripts/release_notes_audience_gate.py``. It
imports ONLY the standard library and never imports ``ops_engine`` or pytest:
the gate must run on a bare release runner that carries neither yaml nor
pydantic (REL-006), so its tests prove the gate through the same constraint.
Each behaviour is proven by a REAL subprocess run of the committed script, not
by reading the script text.

The file runs two ways:
  * ``python3 tests/test_release_notes_audience_gate.py`` — standalone, no
    pytest needed (the ``__main__`` harness below);
  * pytest collection — the same ``test_*`` functions are plain functions with
    plain asserts, so no non-stdlib import is required either way.
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "scripts" / "release_notes_audience_gate.py"
WORKFLOW = REPO_ROOT / ".forgejo" / "workflows" / "forgejo-release.yml"

GATE_CONTENT = GATE.read_text(encoding="utf-8")
WORKFLOW_CONTENT = WORKFLOW.read_text(encoding="utf-8")

# An external-reader entry: prose only, no internal ticket reference. This is
# the tone REL-012 rewrote the real 3.2.0 entry into.
CLEAN_NOTES = """\
The mirror destination is now declared in your configuration as a list, and
every release ships files that can be verified.

### Mirror destinations are now a list

A mirror destination is expressed as a destinations list whose entries carry
a forge, a repo, a role, and a visibility.
"""

# The pre-rewrite register: subheadings that name the author's tracker.
TICKETED_NOTES = """\
A release description that still talks in internal tickets.

### The destination is a list, and the forge is a value (LVC-248)

Three competing shapes existed across the layovers (LVC-248).

### The destination contract from the previous minor, corrected (LVC-247)

### Releases carry files (LVC-250)
"""


def _run_gate(*args, stdin_text=None):
    if stdin_text is not None:
        return subprocess.run(
            [sys.executable, str(GATE), *args],
            input=stdin_text,
            capture_output=True,
            text=True,
        )
    return subprocess.run(
        [sys.executable, str(GATE), *args], capture_output=True, text=True
    )


def _line_of(haystack: str, needle: str) -> int:
    for i, line in enumerate(haystack.splitlines(), start=1):
        if needle in line:
            return i
    raise AssertionError("%r not found in fixture" % needle)


def _write(tmp: str, text: str, name: str = "file") -> str:
    path = Path(tmp) / name
    path.write_text(text, encoding="utf-8")
    return str(path)


# --- Criterion 1: an internal ticket reference is a named refusal -----------


def test_clean_external_reader_notes_pass():
    """Notes with no internal ticket reference pass: exit 0."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, CLEAN_NOTES)
        r = _run_gate(path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout


def test_clean_notes_via_stdin_pass():
    """The absent-file form (stdin) is the normal case and passes."""
    r = _run_gate(stdin_text=CLEAN_NOTES)
    assert r.returncode == 0, r.stdout + r.stderr


def test_internal_ticket_reference_is_refused_red_proof():
    """RED PROOF: a note naming LVC-248/LVC-247/LVC-250 exits 1 and the named
    error quotes each offending token and the line it appears on."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, TICKETED_NOTES)  # name shadows the constant below
        r = _run_gate(path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "ReleaseNotesAudienceError" in r.stderr
    for token in ("LVC-248", "LVC-247", "LVC-250"):
        assert token in r.stderr, r.stderr
    assert "line %d" % _line_of(TICKETED_NOTES, "LVC-248") in r.stderr
    assert "line %d" % _line_of(TICKETED_NOTES, "LVC-247") in r.stderr
    assert "line %d" % _line_of(TICKETED_NOTES, "LVC-250") in r.stderr


def test_external_identifier_prose_passes():
    """GREEN PROOF for the rework finding: a CVE id, an RFC number, an ISO
    format string, and an RPC protocol number are shape-identical to an
    internal ticket reference but are prose the EXTERNAL reader legitimately
    needs (a security fix must be announced by its CVE id; a standards
    citation names an RFC/ISO/PEP/RPC). Each must PASS — exit 0 — not be
    mislabelled the author's tracker.

    This is deliberately a pure Layer-1 scan (no --forbid-file): the exclusion
    lives in the gate, not in an org vocabulary. ops-engine is the template and
    knows no org; CVE/RFC/ISO/PEP/RPC are universal identifier classes, so they
    are excluded in Layer 1, while an organisation's own ticket prefixes must
    keep flowing through --forbid-file from Layer 2.
    """
    notes = (
        "This release fixes CVE-2026-12345, a severity-9 advisory in the SBOM "
        "scanner, adopts RFC-5322 date formatting, ships an ISO-8601 timestamp, "
        "follows PEP-8, and drops RPC-2 (gRPC-2) framing.\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, notes)
        r = _run_gate(path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout


# --- Criterion 2: optional --forbid-file, absent = normal, malformed = named -


def test_absent_forbid_file_is_the_proven_normal_case():
    """ops-engine ships no organisation vocabulary: the gate is invoked without
    --forbid-file and clean notes pass. The workflow below never passes the
    flag either."""
    r = _run_gate(stdin_text=CLEAN_NOTES)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--forbid-file" not in _gate_step_block()


def test_forbid_file_term_found_is_a_named_refusal():
    """A forbid file term present in the notes is refused with the term and
    line named."""
    notes = "The public docs describe how capacium resolves each destination.\n"
    with tempfile.TemporaryDirectory() as tmp:
        notes_path = _write(tmp, notes, "notes.md")
        forbid_path = _write(tmp, "capacium\n", "forbid.txt")
        r = _run_gate("--forbid-file", forbid_path, notes_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "ForbiddenVocabularyError" in r.stderr
    assert "capacium" in r.stderr
    assert "line 1" in r.stderr


def test_forbid_file_term_absent_still_passes():
    """A well-formed forbid file whose terms do not appear changes nothing."""
    with tempfile.TemporaryDirectory() as tmp:
        notes_path = _write(tmp, CLEAN_NOTES, "notes.md")
        forbid_path = _write(tmp, "capacium\n", "forbid.txt")
        r = _run_gate("--forbid-file", forbid_path, notes_path)
    assert r.returncode == 0, r.stdout + r.stderr


def test_malformed_forbid_file_is_a_named_refusal():
    """A forbid file whose line is not one whitespace-free term is refused with
    a named error — never silently skipped."""
    with tempfile.TemporaryDirectory() as tmp:
        notes_path = _write(tmp, CLEAN_NOTES, "notes.md")
        forbid_path = _write(tmp, "two words\n", "forbid.txt")
        r = _run_gate("--forbid-file", forbid_path, notes_path)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "ForbiddenVocabularyError" in r.stderr
    assert "not one term per line" in r.stderr
    assert "line 1" in r.stderr


def test_missing_forbid_file_is_a_named_refusal():
    """A forbid file that is NAMED but does not resolve is a named refusal, not
    a silent release without the vocabulary."""
    with tempfile.TemporaryDirectory() as tmp:
        notes_path = _write(tmp, CLEAN_NOTES)
        r = _run_gate("--forbid-file", str(Path(tmp) / "nope"), notes_path)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "ForbiddenVocabularyError" in r.stderr


# --- Criterion 3: the gate is stdlib-only and wired before release creation -


def test_gate_runs_without_site_packages():
    """The script executes under `python3 -S`: no site-packages module (yaml,
    pydantic, ops_engine) may be imported, exactly the bare-runner constraint
    REL-006 established."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, CLEAN_NOTES)
        r = subprocess.run(
            [sys.executable, "-S", str(GATE), path],
            capture_output=True,
            text=True,
        )
    assert r.returncode == 0, r.stdout + r.stderr
    # Under `-S` the whole standard library still imports (argparse, re, sys,
    # pathlib). What must be absent is any `import` OF ops_engine at all — the
    # script contains no import/`from ... import ... ops_engine` at module
    # scope. (Its docstring names ops_engine only in prose, so we match the
    # actual import statements, not the bare word.)
    assert "import ops_engine" not in GATE_CONTENT
    assert "ops_engine import" not in GATE_CONTENT
    assert re.search(r"^import\s+ops_engine\b", GATE_CONTENT, re.MULTILINE) is None


def _gate_step_block() -> str:
    """The audience-gate step block, from its `- name:` line to the next step."""
    start = WORKFLOW_CONTENT.index(
        "- name: Gate the release notes for an external audience"
    )
    remainder = WORKFLOW_CONTENT[start:]
    end = re.search(r"^      - name: ", remainder, re.MULTILINE)
    return remainder[: end.start()] if end else remainder


def test_workflow_runs_the_gate_on_extracted_notes_before_release_creation():
    """forgejo-release.yml invokes the committed gate script on the notes the
    extraction step produced, and the gate step sits between the extraction and
    the Create Release step — a failing gate fails the release run before any
    release object exists on either forge."""
    block = _gate_step_block()
    assert "python3 scripts/release_notes_audience_gate.py" in block
    assert "steps.notes.outputs.notes" in block
    notes_idx = WORKFLOW_CONTENT.index("Extract release notes from CHANGELOG")
    gate_idx = WORKFLOW_CONTENT.index(
        "- name: Gate the release notes for an external audience"
    )
    create_idx = WORKFLOW_CONTENT.index("Create Release")
    assert notes_idx < gate_idx < create_idx


def test_workflow_invokes_the_committed_script_not_an_inline_copy():
    """The gate logic lives in scripts/, not duplicated in the workflow: the
    step names the script path and the workflow contains no inline copy of the
    gate's refusal logic."""
    assert "release_notes_audience_gate.py" in WORKFLOW_CONTENT


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
