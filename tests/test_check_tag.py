"""Tests for version-sync check-tag mode (FFR-600-8)."""

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "version-sync.py"
_spec = importlib.util.spec_from_file_location("version_sync", _SCRIPT)
version_sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(version_sync)

PrereleaseNotAllowedError = version_sync.PrereleaseNotAllowedError
check_tag = version_sync.check_tag
_is_prerelease = version_sync._is_prerelease


def _make_repo(tmp_path, version="2.4.3"):
    repo = tmp_path
    (repo / ".version.yaml").write_text(
        "schema: 1\n"
        "source_of_truth: version.txt\n"
        "locations:\n"
        "  - path: version.txt\n"
        "    pattern: '^(\\d+\\.\\d+\\.\\d+(?:-[0-9A-Za-z.-]+)?)$'\n",
        encoding="utf-8",
    )
    (repo / "version.txt").write_text(f"{version}\n", encoding="utf-8")
    return repo


def test_check_tag_strips_v_prefix(tmp_path):
    repo = _make_repo(tmp_path, "2.4.3")
    assert check_tag(repo, "v2.4.3", warnings_as_errors=False) == 0


def test_check_tag_prerelease_gated_by_default(tmp_path):
    repo = _make_repo(tmp_path, "2.4.3-rc.1")
    with pytest.raises(PrereleaseNotAllowedError):
        check_tag(repo, "v2.4.3-rc.1", warnings_as_errors=False)


def test_check_tag_prerelease_allowed(tmp_path):
    repo = _make_repo(tmp_path, "2.4.3-rc.1")
    assert (
        check_tag(
            repo,
            "v2.4.3-rc.1",
            warnings_as_errors=False,
            allow_prereleases=True,
        )
        == 0
    )


def test_check_tag_prerelease_tag_gated_even_if_value_stable(tmp_path):
    repo = _make_repo(tmp_path, "2.4.3")
    with pytest.raises(PrereleaseNotAllowedError):
        check_tag(repo, "v2.4.3-rc.1", warnings_as_errors=False)


def test_is_prerelease_classifier():
    assert _is_prerelease("2.4.3-rc.1")
    assert _is_prerelease("2.4.3-beta.2")
    assert not _is_prerelease("2.4.3")
