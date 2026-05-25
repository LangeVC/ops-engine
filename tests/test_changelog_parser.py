"""Tests for CORE-006: ChangelogParser."""

from ops_engine.utils.changelog_parser import ChangelogParser


def test_extract_version_notes(sample_changelog):
    notes = ChangelogParser.extract_version_notes(sample_changelog, "1.0.0")
    assert "Feature A" in notes
    assert "Fixed a crash" in notes


def test_extract_version_notes_with_v_prefix(sample_changelog):
    notes = ChangelogParser.extract_version_notes(sample_changelog, "v1.0.0")
    assert "Feature A" in notes


def test_extract_version_not_found(sample_changelog):
    notes = ChangelogParser.extract_version_notes(sample_changelog, "99.0.0")
    assert notes == ""


def test_extract_from_empty_content():
    assert ChangelogParser.extract_from_file("", "1.0.0") == ""
    assert ChangelogParser.extract_from_file(None, "1.0.0") == ""


def test_list_versions(sample_changelog):
    versions = ChangelogParser.list_versions(sample_changelog)
    assert "1.0.0" in versions
    assert "0.9.0" in versions


def test_extract_last_version_section(sample_changelog):
    notes = ChangelogParser.extract_version_notes(sample_changelog, "0.9.0")
    assert "Beta feature" in notes


def test_various_header_formats():
    content = """## [2.0.0] — Breaking Changes

### Breaking
- Removed old API

## mypackage v1.5.0

### Added
- New endpoint
"""
    assert "Removed old API" in ChangelogParser.extract_version_notes(content, "2.0.0")
    assert "New endpoint" in ChangelogParser.extract_version_notes(content, "1.5.0")
