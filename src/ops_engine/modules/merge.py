"""CORE-003: MergeHandler — Auto-merge PRs when CI is green and label is present."""

import logging
from typing import Any

from ops_engine.adapters.base import ForgeAdapter
from ops_engine.config_loader import MergeConfig

logger = logging.getLogger(__name__)


class MergeHandler:
    """Automatically merges PRs that have the trigger label and passing CI."""

    @staticmethod
    async def process_event(
        adapter: ForgeAdapter, event: dict[str, Any], config: MergeConfig
    ) -> None:
        if not config.enabled:
            return

        event_type = event.get("event_type")
        repo = event.get("repo")
        raw = event.get("raw", {})

        if not repo:
            return

        # We react to check_suite / check_run completion, or status events
        if event_type in ("check_suite", "check_run", "status"):
            await _handle_ci_event(adapter, repo, raw, config)
        elif event_type == "pull_request" and event.get("action") == "labeled":
            # Also react when the label is added (CI might already be green)
            await _handle_label_event(adapter, repo, raw, config)


async def _handle_ci_event(
    adapter: ForgeAdapter, repo: str, raw: dict[str, Any], config: MergeConfig
) -> None:
    """When CI completes, check if any open PR with the trigger label can be merged."""
    # Extract the commit SHA that was checked
    sha = None
    if "check_suite" in raw:
        suite = raw["check_suite"]
        if suite.get("conclusion") != "success":
            return
        sha = suite.get("head_sha")
    elif "check_run" in raw:
        run = raw["check_run"]
        if run.get("conclusion") != "success":
            return
        sha = run.get("head_sha")
    elif raw.get("state") == "success":
        sha = raw.get("sha") or raw.get("commit", {}).get("sha")
    else:
        return

    if not sha:
        return

    # Find open PRs whose head matches this SHA
    prs = raw.get("check_suite", {}).get("pull_requests", [])
    if not prs:
        prs = raw.get("check_run", {}).get("pull_requests", [])

    for pr_ref in prs:
        pr_number = pr_ref.get("number")
        if pr_number:
            await _try_merge_pr(adapter, repo, pr_number, config)


async def _handle_label_event(
    adapter: ForgeAdapter, repo: str, raw: dict[str, Any], config: MergeConfig
) -> None:
    """When a label is added, check if this PR can be merged."""
    label_name = raw.get("label", {}).get("name", "")
    if label_name != config.trigger_label:
        return

    pr_number = raw.get("pull_request", {}).get("number") or raw.get("number")
    if pr_number:
        await _try_merge_pr(adapter, repo, pr_number, config)


async def _try_merge_pr(
    adapter: ForgeAdapter, repo: str, pr_number: int, config: MergeConfig
) -> None:
    """Attempt to merge a PR if it meets all criteria."""
    try:
        pr = await adapter.get_pull_request(repo, pr_number)
    except Exception as e:
        logger.error(f"Failed to get PR {repo}#{pr_number}: {e}")
        return

    # Check PR is open and not already merged
    if pr.get("state") != "open" or pr.get("merged"):
        return

    # Check trigger label is present
    pr_labels = [lbl.get("name", "") for lbl in pr.get("labels", [])]
    if config.trigger_label not in pr_labels:
        return

    # Check CI status
    head_sha = pr.get("head", {}).get("sha")
    if not head_sha:
        return

    if config.required_checks:
        try:
            status = await adapter.get_ci_status(repo, head_sha)
            # Check individual check runs if available
            statuses = status.get("statuses", [])
            check_map = {s.get("context", ""): s.get("state", "") for s in statuses}

            for required in config.required_checks:
                if check_map.get(required) != "success":
                    logger.debug(
                        f"PR {repo}#{pr_number}: required check '{required}' not passing, skipping merge"
                    )
                    return
        except Exception as e:
            logger.warning(f"Failed to check CI status for {repo}#{pr_number}: {e}")
            return
    else:
        # No specific checks required — verify overall status is success
        try:
            status = await adapter.get_ci_status(repo, head_sha)
            if status.get("state") not in ("success", "pending"):
                # pending is OK if no required_checks specified (maybe no CI configured)
                if status.get("state") == "failure":
                    return
        except Exception:
            pass

    # All checks passed — merge
    logger.info(f"Auto-merging PR {repo}#{pr_number} (method={config.merge_method})")
    try:
        await adapter.merge_pull_request(
            repo, pr_number,
            merge_method=config.merge_method,
            delete_branch=config.delete_branch,
        )
        logger.info(f"Successfully merged PR {repo}#{pr_number}")
    except Exception as e:
        logger.error(f"Failed to merge PR {repo}#{pr_number}: {e}")
