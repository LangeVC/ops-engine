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
    Destination,
    NotificationConfig,
    NotificationChannel,
    MigrationSourceConfig,
    MigrationTargetConfig,
)
# The function is exported as get_runtime_identity so that its name differs from
# its module's name (tests/test_public_surface.py rejects a name equal to a
# reachable submodule); the module stays reachable as ops_engine.runtime_identity.
from .runtime_identity import RuntimeIdentity
from .runtime_identity import runtime_identity as get_runtime_identity
from .modules.triage import TriageHandler
from .modules.dependency_trigger import DependencyTriggerHandler
from .modules.stale_manager import StaleManager
from .modules.dispatcher import CronDispatcher
from .modules.release import ReleaseHandler
from .modules.merge import MergeHandler
from .modules.mirror import MirrorHandler
from .modules.notification import NotificationHandler
from .modules.migration_runner import (
    MigrationRunner,
    MigrationSource,
    LocalDirSource,
    GitRepoSource,
    MigrationFile,
    CheckResult,
    ApplyResult,
    runner_from_config,
)
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
    "Destination",
    "NotificationConfig",
    "NotificationChannel",
    "MigrationSourceConfig",
    "MigrationTargetConfig",
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
    # Migration runner (CORE-007)
    "MigrationRunner",
    "MigrationSource",
    "LocalDirSource",
    "GitRepoSource",
    "MigrationFile",
    "CheckResult",
    "ApplyResult",
    "runner_from_config",
    # Utils
    "ChangelogParser",
    # Runtime identity
    "get_runtime_identity",
]
