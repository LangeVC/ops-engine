"""The 3.5.0 release notes pass the audience gate, and the version is consistent.

The notes are the ``## 3.5.0`` section of CHANGELOG.md as the release workflow
extracts it (``extract_changelog_section``). The gate runs as the workflow runs
it: ``scripts/release_notes_audience_gate.py --ticket-prefixes <file> <notes>``,
with the organisation's tracker prefixes from ``.ops.yaml`` plus ``OE``, the
prefix of the tasks that produced this release (``.ops.yaml`` does not list it).
"""

import re
import subprocess
import sys
from pathlib import Path

import yaml

from ops_engine.modules.release_gates import (
    check_release_notes_audience,
    extract_changelog_section,
)

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "scripts" / "release_notes_audience_gate.py"
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
VERSION = "3.5.0"
PROGRAMME_PREFIX = "OE"


def _prefixes() -> list:
    ops = yaml.safe_load((ROOT / ".ops.yaml").read_text(encoding="utf-8"))
    declared = list(ops["tracker_prefixes"])
    assert declared, ".ops.yaml declares no tracker_prefixes"
    return declared + [p for p in [PROGRAMME_PREFIX] if p not in declared]


def _run_gate(tmp_path: Path, notes: str):
    prefixes = tmp_path / "prefixes.txt"
    prefixes.write_text("\n".join(_prefixes()) + "\n", encoding="utf-8")
    notes_file = tmp_path / "notes.md"
    notes_file.write_text(notes, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(GATE), "--ticket-prefixes", str(prefixes), str(notes_file)],
        capture_output=True,
        text=True,
    )


def test_the_notes_section_exists_and_is_not_empty():
    notes = extract_changelog_section(CHANGELOG, "v%s" % VERSION)
    assert notes.strip()
    assert "###" in notes


def test_the_notes_pass_the_audience_gate_with_the_prefixes_supplied(tmp_path):
    notes = extract_changelog_section(CHANGELOG, "v%s" % VERSION)
    result = _run_gate(tmp_path, notes)
    assert result.returncode == 0, result.stdout + result.stderr
    check_release_notes_audience(notes, _prefixes())


def test_the_gate_refuses_the_same_notes_with_one_ticket_id_added(tmp_path):
    notes = extract_changelog_section(CHANGELOG, "v%s" % VERSION)
    for prefix in (PROGRAMME_PREFIX, _prefixes()[0]):
        result = _run_gate(tmp_path, notes + "\n\nSee %s-123 for details.\n" % prefix)
        assert result.returncode != 0, "%s-123 passed the gate" % prefix
        assert "%s-123" % prefix in result.stdout + result.stderr


def test_pyproject_and_the_newest_changelog_heading_agree_on_the_version():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M).group(1)
    newest = re.search(r"^##\s+v?(\d+\.\d+\.\d+\S*)", CHANGELOG, re.M).group(1)
    assert declared == newest == VERSION
