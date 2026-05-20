from abc import ABC, abstractmethod
from typing import Any, Dict

class ForgeAdapter(ABC):
    """Base adapter for interacting with different Git forges (GitHub, Forgejo)."""

    @abstractmethod
    async def create_comment(self, repo_full_name: str, issue_or_pr_number: int, body: str) -> None:
        pass

    @abstractmethod
    async def add_labels(self, repo_full_name: str, issue_or_pr_number: int, labels: list[str]) -> None:
        pass

    @abstractmethod
    async def parse_webhook(self, headers: Dict[str, str], payload: bytes) -> Dict[str, Any]:
        """Validates the webhook signature and returns a normalized event dictionary."""
        pass

    @abstractmethod
    async def list_issues(self, repo_full_name: str, state: str = "open") -> list[Dict[str, Any]]:
        pass

    @abstractmethod
    async def update_issue(self, repo_full_name: str, issue_number: int, state: str = None, labels: list[str] = None) -> None:
        pass

    @abstractmethod
    async def dispatch_workflow(self, repo_full_name: str, event_type: str, client_payload: Dict[str, Any] = None) -> None:
        pass
