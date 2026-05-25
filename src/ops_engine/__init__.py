"""Ops Engine v2 — Event-driven multi-org release orchestration for GitHub and Forgejo."""

from .core.queue_manager import QueueManager, QueueMetrics
from .core.event_dedup import EventDeduplicator
from .config_loader import (
    OpsEngineConfig,
    OrgConfig,
    RepoConfig,
    AutoTriageConfig,
    StaleManagementConfig,
    WorkflowDispatchConfig,
    DependencyTriggerConfig,
    ReleaseConfig,
    MergeConfig,
    MirrorConfig,
    NotificationConfig,
    NotificationChannel,
)
from .modules.triage import TriageHandler
from .modules.dependency_trigger import DependencyTriggerHandler
from .modules.stale_manager import StaleManager
from .modules.dispatcher import CronDispatcher
from .modules.release import ReleaseHandler
from .modules.merge import MergeHandler
from .modules.mirror import MirrorHandler
from .modules.notification import NotificationHandler
from .utils.changelog_parser import ChangelogParser

__all__ = [
    # Core
    "QueueManager",
    "QueueMetrics",
    "EventDeduplicator",
    # Config
    "OpsEngineConfig",
    "OrgConfig",
    "RepoConfig",
    "AutoTriageConfig",
    "StaleManagementConfig",
    "WorkflowDispatchConfig",
    "DependencyTriggerConfig",
    "ReleaseConfig",
    "MergeConfig",
    "MirrorConfig",
    "NotificationConfig",
    "NotificationChannel",
    # Handlers (v1)
    "TriageHandler",
    "DependencyTriggerHandler",
    "StaleManager",
    "CronDispatcher",
    # Handlers (v2)
    "ReleaseHandler",
    "MergeHandler",
    "MirrorHandler",
    "NotificationHandler",
    # Utils
    "ChangelogParser",
]
