import logging
from ops_engine.adapters.base import ForgeAdapter
from ops_engine.config_loader import WorkflowDispatchConfig

logger = logging.getLogger(__name__)

class CronDispatcher:
    """Dispatches workflows based on a centralized cron configuration."""

    @staticmethod
    async def run(adapter: ForgeAdapter, repo_full_name: str, configs: list[WorkflowDispatchConfig]):
        for config in configs:
            if config.enabled:
                logger.info(f"Dispatching scheduled workflow {config.workflow_name} for {repo_full_name}")
                await adapter.dispatch_workflow(
                    repo_full_name=repo_full_name,
                    event_type=config.workflow_name
                )
