from typing import Any, Dict
from ops_engine.adapters.base import ForgeAdapter
from ops_engine.config_loader import DependencyTriggerConfig

class DependencyTriggerHandler:
    """Handles dispatching events to other repositories when dependencies change."""
    
    @staticmethod
    async def process_event(adapter: ForgeAdapter, event: Dict[str, Any], configs: list[DependencyTriggerConfig]):
        action = event.get("action")
        event_type = event.get("event_type")
        
        # React to releases
        if event_type == "release" and action == "published":
            repo_full_name = event.get("repo")
            release_data = event.get("raw", {}).get("release", {})
            tag_name = release_data.get("tag_name", "unknown")
            
            for config in configs:
                payload = {
                    "source_repo": repo_full_name,
                    "version": tag_name
                }
                await adapter.dispatch_workflow(
                    repo_full_name=config.target_repo,
                    event_type=config.target_event_type,
                    client_payload=payload
                )
