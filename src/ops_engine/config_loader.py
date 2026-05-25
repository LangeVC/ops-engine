"""Config models — Pydantic schemas for ops-engine configuration.

v2: Added ReleaseConfig, MergeConfig, MirrorConfig, NotificationConfig.
"""

from typing import Optional
from pydantic import BaseModel, Field


class StaleManagementConfig(BaseModel):
    days_until_stale: int = Field(default=60)
    days_until_close: int = Field(default=7)
    stale_label: str = Field(default="stale")
    exempt_labels: list[str] = Field(default_factory=list)


class AutoTriageConfig(BaseModel):
    add_needs_triage_label: bool = Field(default=True)
    assign_author: bool = Field(default=False)
    keyword_labels: dict[str, str] = Field(default_factory=dict)


class WorkflowDispatchConfig(BaseModel):
    enabled: bool = Field(default=False)
    cron_schedule: str = Field(default="")
    workflow_name: str = Field(default="")


class DependencyTriggerConfig(BaseModel):
    target_repo: str
    target_event_type: str = Field(default="dependency-update")


# --- v2 Config Models ---


class ReleaseConfig(BaseModel):
    """Configuration for automatic release creation."""
    enabled: bool = Field(default=False)
    trigger: str = Field(default="tag_push")  # "tag_push" | "merge_label" | "both"
    tag_pattern: str = Field(default="v*")
    changelog_path: str = Field(default="CHANGELOG.md")
    draft: bool = Field(default=False)
    create_tag_on_merge: bool = Field(default=False)


class MergeConfig(BaseModel):
    """Configuration for automatic PR merging."""
    enabled: bool = Field(default=False)
    trigger_label: str = Field(default="auto-merge")
    required_checks: list[str] = Field(default_factory=list)
    merge_method: str = Field(default="squash")  # "squash" | "merge" | "rebase"
    delete_branch: bool = Field(default=True)


class MirrorConfig(BaseModel):
    """Configuration for mirror sync verification."""
    enabled: bool = Field(default=False)
    primary_forge: str = Field(default="forgejo")  # "forgejo" | "github"
    mirror_url: str = Field(default="")
    verify_on_push: bool = Field(default=True)
    max_drift_seconds: int = Field(default=300)


class NotificationChannel(BaseModel):
    """A single notification destination."""
    type: str = Field(default="webhook")  # "webhook" | "slack" | "discord"
    url: str = Field(default="")
    events: list[str] = Field(default_factory=lambda: ["release"])
    template: str = Field(default="default")


class NotificationConfig(BaseModel):
    """Configuration for event notifications."""
    enabled: bool = Field(default=False)
    channels: list[NotificationChannel] = Field(default_factory=list)


# --- Aggregate Configs ---


class RepoConfig(BaseModel):
    stale_management: Optional[StaleManagementConfig] = None
    auto_triage: Optional[AutoTriageConfig] = None
    workflow_dispatches: list[WorkflowDispatchConfig] = Field(default_factory=list)
    dependency_triggers: list[DependencyTriggerConfig] = Field(default_factory=list)
    # v2
    release: Optional[ReleaseConfig] = None
    auto_merge: Optional[MergeConfig] = None
    mirror: Optional[MirrorConfig] = None
    notifications: Optional[NotificationConfig] = None


class OrgConfig(BaseModel):
    stale_management: StaleManagementConfig = Field(default_factory=StaleManagementConfig)
    auto_triage: AutoTriageConfig = Field(default_factory=AutoTriageConfig)
    # v2: org-level defaults for new configs
    release: Optional[ReleaseConfig] = None
    auto_merge: Optional[MergeConfig] = None
    notifications: Optional[NotificationConfig] = None
    repositories: dict[str, RepoConfig] = Field(default_factory=dict)


class OpsEngineConfig(BaseModel):
    orgs: dict[str, OrgConfig] = Field(default_factory=dict)

    def get_repo_config(self, org_name: str, repo_name: str) -> RepoConfig:
        """Returns a resolved RepoConfig merging Org defaults with Repo specifics."""
        org_config = self.orgs.get(org_name, OrgConfig())
        repo_specific = org_config.repositories.get(repo_name, RepoConfig())

        return RepoConfig(
            stale_management=repo_specific.stale_management or org_config.stale_management,
            auto_triage=repo_specific.auto_triage or org_config.auto_triage,
            workflow_dispatches=repo_specific.workflow_dispatches,
            dependency_triggers=repo_specific.dependency_triggers,
            # v2: merge with org defaults
            release=repo_specific.release or org_config.release,
            auto_merge=repo_specific.auto_merge or org_config.auto_merge,
            mirror=repo_specific.mirror,  # no org default (repo-specific only)
            notifications=repo_specific.notifications or org_config.notifications,
        )
