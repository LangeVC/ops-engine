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

# LangeVC's own tracker prefixes, exactly as the release workflow declares them
# (the config layer). The engine itself knows no org; the vocabulary arrives
# from the workflow.
ORG_PREFIXES = "LVC\nOME\nCORE\nLNF\nDST\nREL\nCFG\nFFR\n"

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

# Ordinary external-facing technical prose and universal identifier classes.
# All of these share the code-and-number shape of an internal ticket reference
# yet are exactly the text an EXTERNAL reader needs. Every line must PASS once
# the gate is armed with the organisation's own prefixes only.
EXTERNAL_PROSE_LINES = (
    "Files are now read as UTF-8 and hashed with SHA-256.\n",
    "Connections now use TLS-1.3 over HTTP-2.\n",
    "The payload is encrypted with AES-256 and signed with RSA-2048.\n",
    "This release fixes CVE-2026-12345, a severity-9 advisory in the SBOM scanner.\n",
    "Dates now follow RFC-5322 and timestamps ship as ISO-8601.\n",
    "The code follows PEP-8 and drops RPC-2 (gRPC-2) framing.\n",
)


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

# The vocabulary helper shared by the tests that arm the gate with the org's
# prefixes: the engine is fed the same prefix file the workflow builds.
def _org_prefix_args(tmp):
    return "--ticket-prefixes", _write(tmp, ORG_PREFIXES, "prefixes.txt")


def test_clean_external_reader_notes_pass_with_org_prefixes():
    """Notes with no internal ticket reference pass (exit 0) even when the gate
    is armed with the organisation's own prefixes."""
    with tempfile.TemporaryDirectory() as tmp:
        notes_path = _write(tmp, CLEAN_NOTES, "notes.md")
        pre = _org_prefix_args(tmp)
        r = _run_gate(pre[0], pre[1], notes_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout


def test_clean_notes_via_stdin_pass_with_no_vocabulary():
    """The default (no --ticket-prefixes, no --forbid-file) is the template's
    normal case: the engine holds no organisation vocabulary, so clean notes
    pass."""
    r = _run_gate(stdin_text=CLEAN_NOTES)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout


def test_with_no_vocabulary_the_gate_refuses_nothing():
    """RED PROOF for the rework: with NO --ticket-prefixes supplied, a
    code-and-number token is not classified as an internal ticket reference at
    all — the engine has no organisation prefix to match and refusing nothing
    is the correct default, not a hole. The org's own token therefore passes
    only when the gate is unarmed; when armed it is refused."""
    with tempfile.TemporaryDirectory() as tmp:
        r = _run_gate(stdin_text="A note that mentions LVC-250.\n")
    assert r.returncode == 0, r.stdout + r.stderr

    with tempfile.TemporaryDirectory() as tmp:
        pre = _org_prefix_args(tmp)
        r = _run_gate(pre[0], pre[1], stdin_text="A note that mentions LVC-250.\n")
    assert r.returncode == 1, r.stdout + r.stderr


def test_internal_ticket_reference_is_refused_when_armed_red_proof():
    """RED PROOF: with the organisation's prefixes armed, a note naming
    LVC-248/LVC-247/LVC-250 exits 1 and the named error quotes each offending
    token and the line it appears on."""
    with tempfile.TemporaryDirectory() as tmp:
        notes_path = _write(tmp, TICKETED_NOTES, "notes.md")  # name shadows below
        pre = _org_prefix_args(tmp)
        r = _run_gate(pre[0], pre[1], notes_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "ReleaseNotesAudienceError" in r.stderr
    for token in ("LVC-248", "LVC-247", "LVC-250"):
        assert token in r.stderr, r.stderr
    assert "line %d" % _line_of(TICKETED_NOTES, "LVC-248") in r.stderr
    assert "line %d" % _line_of(TICKETED_NOTES, "LVC-247") in r.stderr
    assert "line %d" % _line_of(TICKETED_NOTES, "LVC-250") in r.stderr
    assert "LVC" in r.stderr


def test_external_identifier_and_technical_prose_passes_when_armed_green_proof():
    """GREEN PROOF for the rework finding: every line of ordinary technical
    prose AND each universal identifier class (CVE an advisory id, RFC/ISO a
    standards number, PEP-8, gRPC) is shape-identical to an internal ticket
    reference but is exactly the text the EXTERNAL reader needs. Armed with
    only the organisation's OWN prefixes (LVC, OME, ...), NONE of these may be
    refused: UTF-8, SHA-256, TLS-1.3, HTTP-2, AES-256, RSA-2048, CVE-...,
    RFC-..., ISO-..., PEP-8 and gRPC all pass — exit 0 — line by line."""
    for line in EXTERNAL_PROSE_LINES:
        with tempfile.TemporaryDirectory() as tmp:
            notes_path = _write(tmp, line, "notes.md")
            pre = _org_prefix_args(tmp)
            r = _run_gate(pre[0], pre[1], notes_path)
        assert r.returncode == 0, (line, r.stdout + r.stderr)


def test_an_organisation_token_is_refused_but_universal_prose_is_not():
    """Side by side, the discriminator is the own-prefix match, nothing else:
    armed with ``LVC`` only, ``LVC-250`` is refused while ``UTF-8`` and
    ``CVE-2026-12345`` pass in the same note."""
    notes = (
        "The notes compute a UTF-8 digest (LVC-250) and fix CVE-2026-12345.\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        notes_path = _write(tmp, notes, "notes.md")
        prefixes_path = _write(tmp, "LVC\n", "prefixes.txt")
        r = _run_gate("--ticket-prefixes", prefixes_path, notes_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "ReleaseNotesAudienceError" in r.stderr
    assert "LVC-250" in r.stderr
    # The refusal names ONLY the org token; it never names UTF-8 or the CVE.
    assert "UTF-8" not in r.stderr
    assert "CVE-2026-12345" not in r.stderr


def test_malformed_ticket_prefix_file_is_a_named_refusal():
    """A --ticket-prefixes file whose line is not one uppercase [A-Z]{2,5}
    token is refused with a named error — never silently skipped."""
    with tempfile.TemporaryDirectory() as tmp:
        notes_path = _write(tmp, CLEAN_NOTES, "notes.md")
        prefixes_path = _write(tmp, "LVC-1\n", "prefixes.txt")  # not a bare prefix
        r = _run_gate("--ticket-prefixes", prefixes_path, notes_path)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "ForbiddenVocabularyError" in r.stderr
    assert "not a tracker prefix" in r.stderr
    assert "line 1" in r.stderr


def test_missing_ticket_prefix_file_is_a_named_refusal():
    """A --ticket-prefixes file that is NAMED but does not resolve is a named
    refusal, not a silent release without the tracker prefixes."""
    with tempfile.TemporaryDirectory() as tmp:
        notes_path = _write(tmp, CLEAN_NOTES, "notes.md")
        r = _run_gate("--ticket-prefixes", str(Path(tmp) / "nope"), notes_path)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "ForbiddenVocabularyError" in r.stderr


def test_workflow_arms_the_org_prefixes_so_own_notes_stay_gated():
    """The workflow step passes --ticket-prefixes pointing at an inline list of
    LangeVC's tracker prefixes — so THIS repository's own notes stay gated even
    though the engine ships no vocabulary. A layover adopting the workflow
    adapts that list to its own organisation."""
    block = _gate_step_block()
    assert "--ticket-prefixes" in block
    assert "release_notes_audience_gate.py" in block


# --- Criterion 2: optional --forbid-file, absent = normal, malformed = named -

# Consistent with the disarmed default, the bare-engine gate passes clean notes
# when neither vocabulary flag is given: the engine ships no org of its own.
def test_absent_vocabulary_flags_is_the_proven_engine_default():
    """ops-engine (the template) ships no organisation vocabulary: with neither
    flag the gate passes clean notes. This repository's REMOTE differs from the
    template: because ops-engine is a LangeVC repository, the workflow below
    arms --ticket-prefixes so this repo's own notes stay gated."""
    r = _run_gate(stdin_text=CLEAN_NOTES)
    assert r.returncode == 0, r.stdout + r.stderr


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


def test_ticket_prefix_and_forbid_term_are_independent():
    """A withhold term that is not a bare prefix still works alongside an armed
    prefix set, and each fires its own named refusal."""
    notes = "capacium ships LVC-250 and the fix covered by CVE-2026-12345.\n"
    with tempfile.TemporaryDirectory() as tmp:
        notes_path = _write(tmp, notes, "notes.md")
        prefixes_path = _write(tmp, ORG_PREFIXES, "prefixes.txt")
        forbid_path = _write(tmp, "capacium\n", "forbid.txt")
        r = _run_gate(
            "--ticket-prefixes", prefixes_path,
            "--forbid-file", forbid_path,
            notes_path,
        )
    assert r.returncode == 1, r.stdout + r.stderr
    assert "ReleaseNotesAudienceError" in r.stderr
    assert "LVC-250" in r.stderr
    assert "ForbiddenVocabularyError" in r.stderr
    assert "capacium" in r.stderr


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
    assert "--ticket-prefixes" in block
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
