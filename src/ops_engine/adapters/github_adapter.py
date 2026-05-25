"""CORE-001: GithubAdapter — Real HTTP implementation via httpx."""

import hashlib
import hmac
import json
import logging
from base64 import b64decode
from typing import Any, Optional

import httpx

from .base import ForgeAdapter

logger = logging.getLogger(__name__)

API_BASE = "https://api.github.com"
RETRY_DELAYS = [1.0, 2.0, 4.0]


class GithubAdapter(ForgeAdapter):
    def __init__(self, token: str, webhook_secret: str):
        self.token = token
        self.webhook_secret = webhook_secret
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=API_BASE,
                headers={
                    "Authorization": f"token {self.token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=30.0,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Make an HTTP request with retry logic."""
        last_exc: Exception | None = None
        for attempt, delay in enumerate(RETRY_DELAYS):
            try:
                resp = await self.client.request(method, path, **kwargs)
                resp.raise_for_status()
                return resp
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (429, 502, 503):
                    logger.warning(f"GitHub {e.response.status_code} on {path}, retry {attempt + 1}")
                    last_exc = e
                    import asyncio
                    await asyncio.sleep(delay)
                    continue
                raise
            except httpx.TransportError as e:
                logger.warning(f"Transport error on {path}, retry {attempt + 1}: {e}")
                last_exc = e
                import asyncio
                await asyncio.sleep(delay)
        raise last_exc  # type: ignore[misc]

    # --- Webhook ---

    async def parse_webhook(self, headers: dict[str, str], payload: bytes) -> dict[str, Any]:
        self._verify_signature(headers, payload)
        event_type = headers.get("x-github-event", "unknown")
        data = json.loads(payload)
        return {
            "source": "github",
            "event_type": event_type,
            "action": data.get("action"),
            "repo": data.get("repository", {}).get("full_name"),
            "sender": data.get("sender", {}).get("login"),
            "delivery_id": headers.get("x-github-delivery"),
            "raw": data,
        }

    def _verify_signature(self, headers: dict[str, str], payload: bytes) -> None:
        sig_header = headers.get("x-hub-signature-256", "")
        if not sig_header or not self.webhook_secret:
            return
        expected = "sha256=" + hmac.new(
            self.webhook_secret.encode(), payload, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig_header, expected):
            raise ValueError("Invalid webhook signature")

    # --- Issues & Comments (v1, now with real HTTP) ---

    async def create_comment(self, repo_full_name: str, issue_or_pr_number: int, body: str) -> None:
        await self._request(
            "POST",
            f"/repos/{repo_full_name}/issues/{issue_or_pr_number}/comments",
            json={"body": body},
        )

    async def add_labels(self, repo_full_name: str, issue_or_pr_number: int, labels: list[str]) -> None:
        await self._request(
            "POST",
            f"/repos/{repo_full_name}/issues/{issue_or_pr_number}/labels",
            json={"labels": labels},
        )

    async def list_issues(self, repo_full_name: str, state: str = "open") -> list[dict[str, Any]]:
        resp = await self._request("GET", f"/repos/{repo_full_name}/issues", params={"state": state, "per_page": 100})
        return resp.json()

    async def update_issue(
        self, repo_full_name: str, issue_number: int,
        state: Optional[str] = None, labels: Optional[list[str]] = None,
    ) -> None:
        data: dict[str, Any] = {}
        if state:
            data["state"] = state
        if labels is not None:
            data["labels"] = labels
        if data:
            await self._request("PATCH", f"/repos/{repo_full_name}/issues/{issue_number}", json=data)

    async def dispatch_workflow(
        self, repo_full_name: str, event_type: str,
        client_payload: Optional[dict[str, Any]] = None,
    ) -> None:
        await self._request(
            "POST",
            f"/repos/{repo_full_name}/dispatches",
            json={"event_type": event_type, "client_payload": client_payload or {}},
        )

    # --- v2: Release, Merge, CI, Files ---

    async def create_release(
        self, repo_full_name: str, tag_name: str, name: str, body: str,
        draft: bool = False, prerelease: bool = False,
    ) -> dict[str, Any]:
        resp = await self._request(
            "POST",
            f"/repos/{repo_full_name}/releases",
            json={
                "tag_name": tag_name,
                "name": name,
                "body": body,
                "draft": draft,
                "prerelease": prerelease,
            },
        )
        return resp.json()

    async def create_tag(
        self, repo_full_name: str, tag_name: str, target: str = "main",
        message: Optional[str] = None,
    ) -> dict[str, Any]:
        if message:
            # Annotated tag: create tag object first, then ref
            tag_obj = await self._request(
                "POST",
                f"/repos/{repo_full_name}/git/tags",
                json={
                    "tag": tag_name,
                    "message": message,
                    "object": target,
                    "type": "commit",
                },
            )
            sha = tag_obj.json()["sha"]
        else:
            sha = target

        resp = await self._request(
            "POST",
            f"/repos/{repo_full_name}/git/refs",
            json={"ref": f"refs/tags/{tag_name}", "sha": sha},
        )
        return resp.json()

    async def merge_pull_request(
        self, repo_full_name: str, pr_number: int,
        merge_method: str = "squash", delete_branch: bool = True,
    ) -> dict[str, Any]:
        resp = await self._request(
            "PUT",
            f"/repos/{repo_full_name}/pulls/{pr_number}/merge",
            json={"merge_method": merge_method},
        )
        result = resp.json()

        if delete_branch:
            try:
                pr_data = await self.get_pull_request(repo_full_name, pr_number)
                branch = pr_data.get("head", {}).get("ref")
                if branch:
                    await self._request("DELETE", f"/repos/{repo_full_name}/git/refs/heads/{branch}")
            except Exception as e:
                logger.warning(f"Failed to delete branch after merge: {e}")

        return result

    async def get_pull_request(self, repo_full_name: str, pr_number: int) -> dict[str, Any]:
        resp = await self._request("GET", f"/repos/{repo_full_name}/pulls/{pr_number}")
        return resp.json()

    async def get_ci_status(self, repo_full_name: str, ref: str) -> dict[str, Any]:
        resp = await self._request("GET", f"/repos/{repo_full_name}/commits/{ref}/status")
        return resp.json()

    async def get_file_content(self, repo_full_name: str, file_path: str, ref: str = "main") -> str:
        resp = await self._request(
            "GET", f"/repos/{repo_full_name}/contents/{file_path}", params={"ref": ref}
        )
        data = resp.json()
        if data.get("encoding") == "base64":
            return b64decode(data["content"]).decode("utf-8")
        return data.get("content", "")

    async def get_latest_commit_sha(self, repo_full_name: str, branch: str = "main") -> str:
        resp = await self._request("GET", f"/repos/{repo_full_name}/git/ref/heads/{branch}")
        return resp.json()["object"]["sha"]

    async def release_exists(self, repo_full_name: str, tag_name: str) -> bool:
        try:
            await self._request("GET", f"/repos/{repo_full_name}/releases/tags/{tag_name}")
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return False
            raise
