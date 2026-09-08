"""REL-022 — the release title gate knows the prerelease form it cut.

The gate refreshes in ``.forgejo/workflows/forgejo-release.yml`` as an inline,
heredoc-delimited Python block (``TITLE_GATE_PY``). It must reject a
malformed prerelease (``v3.5.0-rc``, ``v3.5.0-``, uppercase ``v3.5.0-RC1``)
while accepting the decided lowercase ``v3.5.0-rc1`` prerelease form. "Each by
name" is proven the way the runner experiences it: the run block is rendered
from the YAML, bash -n'd, and the embedded interpreter is extracted and
executed against each title — not a regex copy in this file, which would be a
different artefact from the gate the runner actually runs (REL-004).

This file deliberately imports nothing outside the standard library and never
imports ``ops_engine`` or pytest: the gate must run on a bare release runner
that carries neither yaml nor pydantic (REL-006), so its tests prove the gate
through the same constraint by real subprocess runs. It runs two ways:

  * ``python3 tests/test_release_title_gate.py`` — standalone;
  * pytest collection — the ``test_*`` functions are plain functions with
    plain asserts, so no non-stdlib import is required either way.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".forgejo" / "workflows" / "forgejo-release.yml"
CONTRACT = REPO_ROOT / "CONTRACT.md"

WORKFLOW_CONTENT = WORKFLOW.read_text(encoding="utf-8")
CONTRACT_CONTENT = CONTRACT.read_text(encoding="utf-8")

TITLE_MARKER = "TITLE_GATE_PY"

# The decided prerelease shape the gate must learn is the lowercase "-rcN"
# form: accepted. The three malformed shapes it must keep refusing, each by
# name in stderr, are what REL-022 must not loosen into accepting.
ACCEPTED = "Ops Engine v3.5.0-rc1"
REFUSED = [
    "Ops Engine v3.5.0-rc",   # prerelease marker with no build number
    "Ops Engine v3.5.0-",     # dangling separator
    "Ops Engine v3.5.0-RC1",  # uppercase marker is not the decided form
]

CONTRACT_HEADING = "## Release titles and pre-releases (REL-022)"
QUESTIONS = [
    "**Does a pre-release publish to both forges, or only the canonical one?**",
    "**Is it marked prerelease on the forge, and does anything downstream read that?**",
    "**May a layover pin ever point at one?**",
]


def _extract_py_block(marker: str, workflow_text: str = WORKFLOW_CONTENT) -> str:
    """Return the de-indented body of the heredoc delimited by ``marker``.

    The block runs from the line after the ``<<'MARKER'`` line down to the line
    whose stripped text equals ``MARKER``, exactly as the shell would read it.
    """
    lines = workflow_text.splitlines()
    start = end = None
    for i, line in enumerate(lines):
        if f"<<'{marker}'" in line:
            start = i
        if start is not None and line.strip() == marker and i > start:
            end = i
            break
    assert start is not None and end is not None, f"heredoc {marker} not found"
    return textwrap.dedent("\n".join(lines[start + 1 : end]) + "\n")


def _run_title_gate(*titles: str) -> list:
    """Execute the gate's embedded interpreter exactly once per title."""
    source = _extract_py_block(TITLE_MARKER)
    results = []
    for title in titles:
        results.append(
            subprocess.run(
                [sys.executable, "-c", source, title],
                capture_output=True,
                text=True,
            )
        )
    return results


# --- Criterion 1: the taught prerelease form is accepted; the malformed forms ---
# --- are refused, each holding its name in a named error.                 -------


def test_gate_accepts_the_taught_prerelease_title():
    (ok,) = _run_title_gate(ACCEPTED)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "conforms" in ok.stdout


def test_gate_refuses_the_malformed_prerelease_titles_each_by_name():
    results = _run_title_gate(*REFUSED)
    assert len(results) == len(REFUSED)
    for title, r in zip(REFUSED, results):
        assert r.returncode != 0, f"{title!r} was accepted"
        assert "ReleaseTitleError" in r.stderr, r.stderr
        assert title in r.stderr, f"{title!r} was not named in the refusal"


def test_gate_still_refuses_the_stable_form_variants():
    """The widening must not loosen the stable gate: a plain stable title that
    was accepted stays accepted, and a prerelease suffix that is not the
    decided lower-case rc form stays refused."""
    ok, bad = _run_title_gate("Ops Engine v3.5.0", "Ops Engine v3.5.0-beta")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "conforms" in ok.stdout
    assert bad.returncode != 0
    assert "ReleaseTitleError" in bad.stderr


# --- Criterion 3: the gate is exercised AS THE RUNNER RENDERS IT. -------


def test_run_block_shell_is_well_formed():
    """The step that carries the gate has a run block that parses under bash -n
    after the YAML is rendered to its literal block scalar."""
    block = _title_gate_run_block()
    rc = subprocess.run(["bash", "-n"], input=block, capture_output=True, text=True)
    assert rc.returncode == 0, rc.stderr
    assert rc.stderr == ""


def test_embedded_interpreter_is_what_the_run_block_executes():
    """The extracted ``TITLE_GATE_PY`` block is present in the rendered run block
    of the gate step, so executing the extracted interpreter is executing the
    gate the runner runs — not a copy invented by this test."""
    block = _title_gate_run_block()
    assert TITLE_MARKER in block
    assert "Ops Engine ${TAG_NAME}" in block
    assert _extract_py_block(TITLE_MARKER, block).strip()


# --- Criterion 2: the contract decision and its machine check. ----------


def test_contract_carries_the_rel022_decision_with_answered_questions():
    assert CONTRACT_HEADING in CONTRACT_CONTENT
    section = CONTRACT_CONTENT.split(CONTRACT_HEADING, 1)[1]
    next_heading = "## Test enforcement"
    section_body = section.split(next_heading, 1)[0]
    for q in QUESTIONS:
        assert q in section_body, f"missing verbatim question: {q}"
        answer = _answer_after(section_body, q)
        assert answer and answer.strip(), (
            f"a heading has no non-empty answer: {q}"
        )


def _answer_after(section_body: str, question: str) -> str:
    """Return the text between ``question`` and the next bold heading or the end
    of the section. A blank or whitespace-only answer therefore fails the
    assert in the calling test."""
    tail = section_body.split(question, 1)[1]
    for chunk in tail.split("**"):
        if chunk.strip():
            return chunk
    return ""


def _title_gate_run_block() -> str:
    """Return the literal, de-indented scalar body under the gate step's ``run: |``.

    Rendered how the runner renders it: lines strictly more indented than the
    ``run:`` key are the block scalar body. Slicing on indentation alone — from
    the line after ``run: |`` down to the first line whose indent is not deeper
    than the ``run:`` key's — yields exactly the shell the runner executes, and
    nothing from the neighbouring steps. Feeding that body to ``bash -n`` proves
    it is well formed, and the ``TITLE_GATE_PY`` embedded interpreter it carries
    is the gate this suite executes.
    """
    lines = WORKFLOW_CONTENT.splitlines()
    start = end = None
    for i, line in enumerate(lines):
        if line.strip() == "- name: Gate the release title":
            start = i
            break
    assert start is not None, "title-gate step not found"
    run_idx = None
    for i in range(start, len(lines)):
        if lines[i].lstrip().startswith("run: |"):
            run_idx = i
            break
    assert run_idx is not None, "title-gate step has no run block scalar"
    indent = len(lines[run_idx]) - len(lines[run_idx].lstrip())
    body = []
    for i in range(run_idx + 1, len(lines)):
        line = lines[i]
        if line.strip() == "":
            body.append("")
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        if line.lstrip().startswith("- name:"):
            break
        body.append(line)
    return textwrap.dedent("\n".join(body)) + "\n"


# ---------------------------------------------------------------- runner ----

if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print(f"\n{len([n for n in globals() if n.startswith('test_')]) - failures} passed, "
          f"{failures} failed")
    sys.exit(1 if failures else 0)
