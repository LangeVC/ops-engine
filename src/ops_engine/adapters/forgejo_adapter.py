"""CORE-001: ForgejoAdapter — Real HTTP implementation via httpx (Gitea-compatible API)."""

import hashlib
import hmac
import json
import logging
from typing import Any, Optional

import httpx

from .base import ForgeAdapter

logger = logging.getLogger(__name__)

RETRY_DELAYS = [1.0, 2.0, 4.0]


class ForgejoAdapter(ForgeAdapter):
    def __init__(self, base_url: str, token: str, webhook_secret: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.webhook_secret = webhook_secret
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=f"{self.base_url}/api/v1",
                headers={
                    "Authorization": f"token {self.token}",
                    "Accept": "application/json",
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
                    logger.warning(f"Forgejo {e.response.status_code} on {path}, retry {attempt + 1}")
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
        event_type = headers.get("x-forgejo-event") or headers.get("x-gitea-event", "unknown")
        data = json.loads(payload)
        return {
            "source": "forgejo",
            "event_type": event_type,
            "action": data.get("action"),
            "repo": data.get("repository", {}).get("full_name"),
            "sender": data.get("sender", {}).get("username"),
            "delivery_id": headers.get("x-forgejo-delivery") or headers.get("x-gitea-delivery"),
            "raw": data,
        }

    def _verify_signature(self, headers: dict[str, str], payload: bytes) -> None:
        sig_header = headers.get("x-forgejo-signature") or headers.get("x-gitea-signature", "")
        if not sig_header or not self.webhook_secret:
            return
        expected = hmac.new(
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
        # Forgejo needs label IDs, not names — resolve first
        resp = await self._request("GET", f"/repos/{repo_full_name}/labels")
        all_labels = resp.json()
        label_map = {lbl["name"]: lbl["id"] for lbl in all_labels}
        label_ids = [label_map[name] for name in labels if name in label_map]
        if label_ids:
            await self._request(
                "POST",
                f"/repos/{repo_full_name}/issues/{issue_or_pr_number}/labels",
                json={"labels": label_ids},
            )

    async def list_issues(self, repo_full_name: str, state: str = "open") -> list[dict[str, Any]]:
        resp = await self._request(
            "GET", f"/repos/{repo_full_name}/issues", params={"state": state, "limit": 50}
        )
        return resp.json()

    async def update_issue(
        self, repo_full_name: str, issue_number: int,
        state: Optional[str] = None, labels: Optional[list[str]] = None,
    ) -> None:
        data: dict[str, Any] = {}
        if state:
            data["state"] = state
        if data:
            await self._request("PATCH", f"/repos/{repo_full_name}/issues/{issue_number}", json=data)

    async def dispatch_workflow(
        self, repo_full_name: str, event_type: str,
        client_payload: Optional[dict[str, Any]] = None,
    ) -> None:
        # Forgejo uses repository_dispatch via the API
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
        resp = await self._request(
            "POST",
            f"/repos/{repo_full_name}/tags",
            json={
                "tag_name": tag_name,
                "target": target,
                "message": message or "",
            },
        )
        return resp.json()

    async def merge_pull_request(
        self, repo_full_name: str, pr_number: int,
        merge_method: str = "squash", delete_branch: bool = True,
    ) -> dict[str, Any]:
        # Forgejo merge method mapping
        do_map = {"squash": "squash", "merge": "merge", "rebase": "rebase-merge"}
        resp = await self._request(
            "POST",
            f"/repos/{repo_full_name}/pulls/{pr_number}/merge",
            json={
                "Do": do_map.get(merge_method, "squash"),
                "delete_branch_after_merge": delete_branch,
            },
        )
        return resp.json() if resp.content else {}

    async def get_pull_request(self, repo_full_name: str, pr_number: int) -> dict[str, Any]:
        resp = await self._request("GET", f"/repos/{repo_full_name}/pulls/{pr_number}")
        return resp.json()

    async def get_ci_status(self, repo_full_name: str, ref: str) -> dict[str, Any]:
        resp = await self._request("GET", f"/repos/{repo_full_name}/commits/{ref}/status")
        return resp.json()

    async def get_file_content(self, repo_full_name: str, file_path: str, ref: str = "main") -> str:
        resp = await self._request(
            "GET", f"/repos/{repo_full_name}/raw/{file_path}", params={"ref": ref}
        )
        return resp.text

    async def get_latest_commit_sha(self, repo_full_name: str, branch: str = "main") -> str:
        resp = await self._request("GET", f"/repos/{repo_full_name}/branches/{branch}")
        return resp.json()["commit"]["id"]

    async def release_exists(self, repo_full_name: str, tag_name: str) -> bool:
        try:
            resp = await self._request("GET", f"/repos/{repo_full_name}/releases/tags/{tag_name}")
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return False
            raise

    async def create_pull_request(
        self, repo_full_name: str, title: str, body: str,
        head: str, base: str, labels: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": title,
            "body": body,
            "head": head,
            "base": base,
        }
        # Forgejo supports label IDs on PR creation — resolve names to IDs
        if labels:
            resp = await self._request("GET", f"/repos/{repo_full_name}/labels")
            all_labels = resp.json()
            label_map = {lbl["name"]: lbl["id"] for lbl in all_labels}
            label_ids = [label_map[name] for name in labels if name in label_map]
            if label_ids:
                payload["labels"] = label_ids
        resp = await self._request(
            "POST", f"/repos/{repo_full_name}/pulls", json=payload,
        )
        return resp.json()

    async def list_pull_requests(
        self, repo_full_name: str, state: str = "open",
        head: str | None = None, base: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"state": state, "limit": 50}
        # Forgejo doesn't support head/base query params natively — filter client-side
        resp = await self._request("GET", f"/repos/{repo_full_name}/pulls", params=params)
        pulls = resp.json()
        if head:
            pulls = [p for p in pulls if p.get("head", {}).get("ref") == head]
        if base:
            pulls = [p for p in pulls if p.get("base", {}).get("ref") == base]
        return pulls
