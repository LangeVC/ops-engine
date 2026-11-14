"""The 3.6.1 release is prepared: version, short notes, audience gate.

The notes are the ``## 3.6.1`` section of CHANGELOG.md as the release workflow
extracts it (``extract_changelog_section``). The gate runs as the workflow runs
it: ``scripts/release_notes_audience_gate.py --ticket-prefixes <file> <notes>``,
with the organisation's tracker prefixes from ``.ops.yaml`` plus ``OE``, the
prefix of the tasks that produced this release (``.ops.yaml`` does not list it).
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from ops_engine.modules.release_gates import (
    check_release_notes_audience,
    check_release_title,
    extract_changelog_section,
)

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "scripts" / "release_notes_audience_gate.py"
VERSION_SYNC = ROOT / "scripts" / "version-sync.py"
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
VERSION = "3.6.1"
PROGRAMME_PREFIX = "OE"
MAX_NOTES_CHARS = 1500
MAX_SECTIONS = 3


def _prefixes() -> list:
    ops = yaml.safe_load((ROOT / ".ops.yaml").read_text(encoding="utf-8"))
    declared = list(ops["tracker_prefixes"])
    assert declared, ".ops.yaml declares no tracker_prefixes"
    return declared + [p for p in [PROGRAMME_PREFIX] if p not in declared]


def _notes() -> str:
    return extract_changelog_section(CHANGELOG, "v%s" % VERSION)


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


def _version_sync(*args: str):
    return subprocess.run(
        [sys.executable, str(VERSION_SYNC), *args, "--repo", str(ROOT)],
        capture_output=True,
        text=True,
    )


def test_version_sync_check_passes_at_the_release_version():
    result = _version_sync("check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert VERSION in result.stdout


def test_the_release_tag_matches_the_declared_version():
    result = _version_sync("check-tag", "v%s" % VERSION)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_notes_section_exists_and_is_short():
    notes = _notes()
    assert notes.strip()
    assert len(notes) < MAX_NOTES_CHARS, len(notes)
    sections = re.findall(r"^### ", notes, re.M)
    assert 1 <= len(sections) <= MAX_SECTIONS, len(sections)


def test_the_notes_name_what_3_6_1_changes():
    notes = _notes().lower()
    assert "credential_variable" in notes and "optional" in notes
    assert "forge" in notes
    assert "loader" in notes
    assert ".ops.yaml" in notes


def test_no_ticket_id_appears_in_the_notes():
    notes = _notes()
    for prefix in _prefixes():
        assert not re.search(r"\b%s-\d+" % re.escape(prefix), notes), prefix


def test_the_notes_pass_the_audience_gate_with_the_prefixes_supplied(tmp_path):
    notes = _notes()
    result = _run_gate(tmp_path, notes)
    assert result.returncode == 0, result.stdout + result.stderr
    check_release_notes_audience(notes, _prefixes())


def test_the_gate_refuses_the_same_notes_with_one_ticket_id_added(tmp_path):
    notes = _notes()
    for prefix in (PROGRAMME_PREFIX, _prefixes()[0]):
        result = _run_gate(tmp_path, notes + "\n\nSee %s-123 for details.\n" % prefix)
        assert result.returncode != 0, "%s-123 passed the gate" % prefix
        assert "%s-123" % prefix in result.stdout + result.stderr


def test_pyproject_and_the_newest_changelog_heading_agree_on_the_version():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M).group(1)
    newest = re.search(r"^##\s+v?(\d+\.\d+\.\d+\S*)", CHANGELOG, re.M).group(1)
    assert declared == newest == VERSION


def test_the_missing_section_is_refused_by_name():
    with pytest.raises(Exception) as excinfo:
        extract_changelog_section(CHANGELOG, "v9.9.9")
    assert "9.9.9" in str(excinfo.value)


def test_the_readme_version_badge_follows_the_declared_version():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M).group(1)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    badge = re.search(r'badge/version-([^-"]+)-[a-z]+\.svg" alt="Version ([^"]+)"', readme)
    assert badge, "README.md carries no version badge"
    assert badge.group(1) == badge.group(2) == declared
    assert declared == VERSION


def test_the_readme_names_the_public_mirror():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "git.langevc.com/langevc/ops-engine" in readme
    assert re.search(r"GitHub repository is a mirror", readme)
    assert "github.com/LangeVC/ops-engine" in readme
    assert re.search(r"3\.6\.1[^\n]*\bmirror\b|\bmirror\b[^\n]*3\.6\.1", readme)


def test_the_release_title_policy_matches_the_title_the_workflow_renders():
    declaration = yaml.safe_load((ROOT / ".version.yaml").read_text(encoding="utf-8"))
    policy = declaration["release_title_policy"]
    product = policy["product_name"]
    pattern = policy["pattern"].replace("{product}", re.escape(product))
    workflow = (ROOT / ".forgejo" / "workflows" / "forgejo-release.yml").read_text(
        encoding="utf-8"
    )
    templates = sorted(set(re.findall(r'NAME_TEMPLATE:\s*"([^"]*)"', workflow)))
    assert templates, "the release workflow declares no NAME_TEMPLATE"
    for template in templates:
        title = check_release_title("v%s" % VERSION, name_template=template)
        assert title == "%s v%s" % (product, VERSION), title
        assert re.match(pattern, title), title
    assert re.match(pattern, check_release_title("v%s-rc1" % VERSION, name_template=templates[0]))
    assert not re.match(pattern, check_release_title("v%s" % VERSION, prefix="Other"))
