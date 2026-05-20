import json
import logging
from typing import Any, Dict
from .base import ForgeAdapter

logger = logging.getLogger(__name__)

class GithubAdapter(ForgeAdapter):
    def __init__(self, token: str, webhook_secret: str):
        self.token = token
        self.webhook_secret = webhook_secret

    async def create_comment(self, repo_full_name: str, issue_or_pr_number: int, body: str) -> None:
        logger.info(f"GitHub: Creating comment on {repo_full_name}#{issue_or_pr_number}")
        # API Implementation here

    async def add_labels(self, repo_full_name: str, issue_or_pr_number: int, labels: list[str]) -> None:
        logger.info(f"GitHub: Adding labels {labels} to {repo_full_name}#{issue_or_pr_number}")
        # API Implementation here

    async def parse_webhook(self, headers: Dict[str, str], payload: bytes) -> Dict[str, Any]:
        event_type = headers.get("x-github-event", "unknown")
        data = json.loads(payload)
        
        return {
            "source": "github",
            "event_type": event_type,
            "action": data.get("action"),
            "repo": data.get("repository", {}).get("full_name"),
            "sender": data.get("sender", {}).get("login"),
            "raw": data
        }
