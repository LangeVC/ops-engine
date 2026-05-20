from typing import Dict, Optional, List
from pydantic import BaseModel, Field

class StaleManagementConfig(BaseModel):
    days_until_stale: int = Field(default=60)
    days_until_close: int = Field(default=7)
    stale_label: str = Field(default="stale")
    exempt_labels: List[str] = Field(default_factory=list)

class AutoTriageConfig(BaseModel):
    add_needs_triage_label: bool = Field(default=True)
    assign_author: bool = Field(default=False)
    keyword_labels: Dict[str, str] = Field(default_factory=dict) # e.g. {"bug": "bug", "feat": "enhancement"}

class WorkflowDispatchConfig(BaseModel):
    enabled: bool = Field(default=False)
    cron_schedule: str = Field(default="")
    workflow_name: str = Field(default="")

class DependencyTriggerConfig(BaseModel):
    target_repo: str
    target_event_type: str = Field(default="dependency-update")

class RepoConfig(BaseModel):
    stale_management: Optional[StaleManagementConfig] = None
    auto_triage: Optional[AutoTriageConfig] = None
    workflow_dispatches: List[WorkflowDispatchConfig] = Field(default_factory=list)
    dependency_triggers: List[DependencyTriggerConfig] = Field(default_factory=list)

class OrgConfig(BaseModel):
    stale_management: StaleManagementConfig = Field(default_factory=StaleManagementConfig)
    auto_triage: AutoTriageConfig = Field(default_factory=AutoTriageConfig)
    repositories: Dict[str, RepoConfig] = Field(default_factory=dict)

class OpsEngineConfig(BaseModel):
    orgs: Dict[str, OrgConfig] = Field(default_factory=dict)

    def get_repo_config(self, org_name: str, repo_name: str) -> RepoConfig:
        """
        Returns a resolved RepoConfig merging Org defaults with Repo specifics.
        """
        org_config = self.orgs.get(org_name, OrgConfig())
        repo_specific = org_config.repositories.get(repo_name, RepoConfig())

        # Resolve Stale Management
        stale_cfg = repo_specific.stale_management or org_config.stale_management
        
        # Resolve Triage
        triage_cfg = repo_specific.auto_triage or org_config.auto_triage

        return RepoConfig(
            stale_management=stale_cfg,
            auto_triage=triage_cfg,
            workflow_dispatches=repo_specific.workflow_dispatches,
            dependency_triggers=repo_specific.dependency_triggers
        )
