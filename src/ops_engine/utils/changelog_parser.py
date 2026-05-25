"""CORE-006: ChangelogParser — Extract release notes from CHANGELOG.md."""

import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class ChangelogParser:
    """Parses CHANGELOG.md and extracts release notes for a specific version."""

    # Matches headers like: ## v1.0.0, ## [1.0.0], ## Package v1.0.0 — Title (date)
    HEADER_PATTERN = re.compile(
        r"^##\s+"
        r"(?:\[)?(?:[\w-]+\s+)?v?"
        r"(\d+\.\d+\.\d+(?:[-.\w]*)?)"
        r"(?:\])?"
        r".*$",
        re.MULTILINE,
    )

    @classmethod
    def extract_version_notes(cls, changelog_content: str, version: str) -> str:
        """Extract release notes for a specific version from CHANGELOG content.

        Args:
            changelog_content: Full text of CHANGELOG.md
            version: Version string (e.g. "1.0.0" or "v1.0.0")

        Returns:
            Markdown string of release notes, or empty string if not found.
        """
        # Normalize: strip leading 'v'
        version = version.lstrip("v")

        matches = list(cls.HEADER_PATTERN.finditer(changelog_content))
        if not matches:
            logger.debug("No version headers found in CHANGELOG content")
            return ""

        target_match = None
        target_idx = -1
        for idx, m in enumerate(matches):
            if m.group(1) == version:
                target_match = m
                target_idx = idx
                break

        if target_match is None:
            logger.debug(f"Version {version} not found in CHANGELOG")
            return ""

        start = target_match.end()
        if target_idx + 1 < len(matches):
            end = matches[target_idx + 1].start()
        else:
            end = len(changelog_content)

        notes = changelog_content[start:end].strip()
        return notes

    @classmethod
    def extract_from_file(cls, file_content: str, version: str) -> str:
        """Convenience wrapper: extract from file content, handle errors gracefully."""
        if not file_content:
            return ""
        try:
            return cls.extract_version_notes(file_content, version)
        except Exception as e:
            logger.warning(f"Failed to parse CHANGELOG: {e}")
            return ""

    @classmethod
    def list_versions(cls, changelog_content: str) -> list[str]:
        """List all versions found in a CHANGELOG."""
        return [m.group(1) for m in cls.HEADER_PATTERN.finditer(changelog_content)]
