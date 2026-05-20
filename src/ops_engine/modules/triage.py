from typing import Any, Dict
from ops_engine.adapters.base import ForgeAdapter
from ops_engine.config_loader import AutoTriageConfig

class TriageHandler:
    """Handles auto-labeling and triage for new PRs and Issues."""
    
    @staticmethod
    async def process_event(adapter: ForgeAdapter, event: Dict[str, Any], config: AutoTriageConfig):
        action = event.get("action")
        event_type = event.get("event_type")
        
        # Only process newly opened PRs or Issues
        if action != "opened" or event_type not in ["pull_request", "issues"]:
            return

        repo_full_name = event.get("repo")
        raw_data = event.get("raw", {})
        
        # Determine if it's a PR or Issue to get the number and title
        item_data = raw_data.get("pull_request") or raw_data.get("issue")
        if not item_data:
            return

        number = item_data.get("number")
        title = item_data.get("title", "").lower()

        labels_to_add = []

        if config.add_needs_triage_label:
            labels_to_add.append("needs-triage")

        for keyword, label in config.keyword_labels.items():
            if keyword.lower() in title:
                labels_to_add.append(label)

        if labels_to_add:
            await adapter.add_labels(repo_full_name, number, list(set(labels_to_add)))
