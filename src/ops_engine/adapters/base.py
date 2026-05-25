"""CORE-001: ForgeAdapter base — Abstract interface for GitHub and Forgejo APIs."""

from abc import ABC, abstractmethod
from typing import Any, Optional


class ForgeAdapter(ABC):
    """Base adapter for interacting with different Git forges (GitHub, Forgejo)."""

    # --- Existing methods (v1) ---

    @abstractmethod
    async def create_comment(self, repo_full_name: str, issue_or_pr_number: int, body: str) -> None:
        pass

    @abstractmethod
    async def add_labels(self, repo_full_name: str, issue_or_pr_number: int, labels: list[str]) -> None:
        pass

    @abstractmethod
    async def parse_webhook(self, headers: dict[str, str], payload: bytes) -> dict[str, Any]:
        """Validate webhook signature and return a normalized event dict."""
        pass

    @abstractmethod
    async def list_issues(self, repo_full_name: str, state: str = "open") -> list[dict[str, Any]]:
        pass

    @abstractmethod
    async def update_issue(
        self, repo_full_name: str, issue_number: int,
        state: Optional[str] = None, labels: Optional[list[str]] = None,
    ) -> None:
        pass

    @abstractmethod
    async def dispatch_workflow(
        self, repo_full_name: str, event_type: str,
        client_payload: Optional[dict[str, Any]] = None,
    ) -> None:
        pass

    # --- New methods (v2) ---

    @abstractmethod
    async def create_release(
        self, repo_full_name: str, tag_name: str, name: str, body: str,
        draft: bool = False, prerelease: bool = False,
    ) -> dict[str, Any]:
        """Create a release for the given tag."""
        pass

    @abstractmethod
    async def create_tag(
        self, repo_full_name: str, tag_name: str, target: str = "main",
        message: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a git tag (lightweight or annotated)."""
        pass

    @abstractmethod
    async def merge_pull_request(
        self, repo_full_name: str, pr_number: int,
        merge_method: str = "squash", delete_branch: bool = True,
    ) -> dict[str, Any]:
        """Merge a pull request."""
        pass

    @abstractmethod
    async def get_pull_request(self, repo_full_name: str, pr_number: int) -> dict[str, Any]:
        """Get pull request details."""
        pass

    @abstractmethod
    async def get_ci_status(self, repo_full_name: str, ref: str) -> dict[str, Any]:
        """Get combined CI/check status for a git ref."""
        pass

    @abstractmethod
    async def get_file_content(self, repo_full_name: str, file_path: str, ref: str = "main") -> str:
        """Read a file's content from the repository."""
        pass

    @abstractmethod
    async def get_latest_commit_sha(self, repo_full_name: str, branch: str = "main") -> str:
        """Get the latest commit SHA for a branch."""
        pass

    @abstractmethod
    async def release_exists(self, repo_full_name: str, tag_name: str) -> bool:
        """Check if a release for the given tag already exists."""
        pass
