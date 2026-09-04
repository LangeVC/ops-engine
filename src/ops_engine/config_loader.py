"""Config models — Pydantic schemas for ops-engine configuration.

v2: Added ReleaseConfig, MergeConfig, MirrorConfig, NotificationConfig.
v2.1: Added MigrationSourceConfig and MigrationTargetConfig (CORE-007).
v3.2: Added Destination (DST-001) — the forge becomes a value, not a key name.
"""

import warnings
from typing import Any, Optional
from pydantic import BaseModel, Field, ValidationError


_DESTINATION_REMOVAL_VERSION = "4.0.0"


def canonical_org_key(repository: dict[str, Any]) -> str:
    """Derive the canonical org key from a Forgejo webhook ``repository`` mapping.

    The canonical org key is used by every internal lookup (see
    :meth:`OpsEngineConfig.get_repo_config`). It must be derived from a field the
    Forgejo API actually sends — not from the database schema. Measured 2026-08-26
    against git.langevc.com's live API, the webhook ``owner`` object carries no
    ``lower_name``; that is a column of the ``user`` table, not a payload field
    (see LVC-238).

    The source of truth is ``repository.full_name`` (``"org/repo"``): LVC-229's
    evidence recorded the webhook delivering ``full_name = "fusionaize/faigrid"``
    — already lowercase. The org portion is lowercased in case a consumer sends
    display case. ``owner.username`` is the fallback: it is the always-lowercase
    unique handle the API sends. If neither yields an org, this refuses with a
    named error rather than keying on a display-only field (``owner.login``,
    ``owner.full_name``) or a free-form display name.
    """
    if not isinstance(repository, dict):
        raise ValueError(
            "cannot derive canonical org key: repository is not a mapping"
        )

    full_name = repository.get("full_name")
    if isinstance(full_name, str) and "/" in full_name:
        org, _sep, _rest = full_name.partition("/")
        if org:
            return org.lower()

    owner = repository.get("owner")
    if isinstance(owner, dict):
        username = owner.get("username")
        if isinstance(username, str) and username:
            return username.lower()

    raise ValueError(
        "cannot derive canonical org key: repository carries no 'full_name' "
        "with an 'org/repo' form and no owner with a 'username'; "
        "refusing to key on a display-only field"
    )


class ConfigSectionError(TypeError):
    """A config section failed to resolve to a typed model.

    Identifies the exact section (and org/repo scope) that is missing, malformed,
    or holds a raw value instead of a typed config model. Raised instead of
    silently falling back to a default so a raw dict can never reach an
    ``.enabled`` access downstream.
    """

    def __init__(
        self,
        section: str,
        *,
        org_name: Optional[str] = None,
        repo_name: Optional[str] = None,
        detail: str = "",
    ) -> None:
        self.section = section
        self.org_name = org_name
        self.repo_name = repo_name
        self.detail = detail
        scope: list[str] = []
        if org_name is not None:
            scope.append(f"org {org_name!r}")
        if repo_name is not None:
            scope.append(f"repo {repo_name!r}")
        scope.append(f"section {section!r}")
        message = " -> ".join(scope) + " is not a valid typed config section"
        if detail:
            message += f": {detail}"
        super().__init__(message)


class StaleManagementConfig(BaseModel):
    days_until_stale: int = Field(default=60)
    days_until_close: int = Field(default=7)
    stale_label: str = Field(default="stale")
    exempt_labels: list[str] = Field(default_factory=list)


# ── Health monitor (scheduled probes; CORE-006) ───────────────────────────────

class HealthCheck(BaseModel):
    """A single probe definition."""
    name: str
    url: str
    method: str = Field(default="GET")
    timeout_seconds: int = Field(default=10)
    expect_status: int = Field(default=200)
    # Optional JSON-path check: response[field] == value (top-level fields only).
    expect_json_field: Optional[str] = None
    expect_json_value: Optional[str] = None
    # Extra request headers. The probe always sets a sensible default
    # User-Agent so Cloudflare-fronted endpoints don't 403 the python-urllib
    # default; override per-check here if needed.
    headers: dict[str, str] = Field(default_factory=dict)


class HealthSink(BaseModel):
    """Where to write health-check results.

    Sink types:
      - "stdout"        — one-line JSON summary to STDOUT (default, always safe).
      - "file"          — append JSONL to ``path`` (SHOULD NOT be inside the
                          source repo; use a CI runner tmp path or external volume).
      - "webhook"       — POST the JSON result to ``url``.
      - "github_issue"  — only on failure, create/update a labeled issue via the
                          configured GitHub adapter (avoids repo pollution).
    """
    type: str = Field(default="stdout")
    # file
    path: Optional[str] = None
    # webhook
    url: Optional[str] = None
    headers: dict[str, str] = Field(default_factory=dict)
    # github_issue
    issue_label: str = Field(default="health-alert")
    issue_title: str = Field(default="Health Check Failed")
    only_on_failure: bool = Field(default=True)


class HealthMonitorConfig(BaseModel):
    """Configuration for scheduled health probes (HealthMonitor module).

    Drop this section into your org-layover config.yml to enable health monitoring
    without writing handler code. Repo-layover does NOT override this (org-only).
    """
    enabled: bool = Field(default=False)
    checks: list[HealthCheck] = Field(default_factory=list)
    sinks: list[HealthSink] = Field(default_factory=lambda: [HealthSink(type="stdout")])
    # On any failed check, exit the runner with non-zero status (CI signal).
    fail_run_on_error: bool = Field(default=True)


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


class Destination(BaseModel):
    """A single destination a repository publishes to.

    The forge is a VALUE, not a key name (DST-001): ``mirror.github`` encoded the
    forge in its key, which is why a GitHub-only or GitLab adopter could not
    express a destination. This model replaces the three competing mirror shapes
    — ``mirror.github`` + ``visibility``, ``mirror_url`` + ``primary_forge``, and
    an absent ``mirror`` section — with one list form:

      destinations:
        - forge: github        # github | forgejo | gitlab | local
          repo: LangeVC/ops-engine
          role: mirror         # mirror | release | replica
          visibility: public

    ``role`` distinguishes the kind of destination: ``mirror`` (read-only
    distribution copy), ``release`` (publishes release artifacts), ``replica``
    (a full transactional copy). ``visibility`` is the per-repo public/private
    classification LVC-247 CFG-006/007 reads.
    """

    forge: str = Field(default="github")  # "github" | "forgejo" | "gitlab" | "local"
    repo: str
    role: str = Field(default="mirror")  # "mirror" | "release" | "replica"
    visibility: str = Field(default="")  # "public" | "private" | ""


class MirrorConfig(BaseModel):
    """Configuration for mirror sync verification.

    As of DST-001 the mirror destination is expressed by ``RepoConfig.destinations``
    (a :class:`Destination` list). The fields below remain only as DEPRECATED
    aliases whose values resolve into that list via
    :meth:`RepoConfig.resolve_destinations`; reading them emits a
    ``DeprecationWarning`` naming the removal version. They are no longer the
    canonical form.
    """
    enabled: bool = Field(default=False)
    primary_forge: str = Field(default="forgejo")  # DEPRECATED alias (4.0.0)
    mirror_url: str = Field(default="")            # DEPRECATED alias (4.0.0)
    verify_on_push: bool = Field(default=True)
    max_drift_seconds: int = Field(default=300)
    # Layer-2 mirror destination, declared here so config.yml's ``mirror.github``
    # ("owner/name") and ``mirror.visibility`` are no longer dropped silently.
    github: str = Field(default="")                # DEPRECATED alias (4.0.0)
    visibility: str = Field(default="")  # "public" | "private" | ""  (DEPRECATED alias)


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


# ── Migration runner (forward-only SQL migrations; CORE-007) ─────────────────


class MigrationSourceConfig(BaseModel):
    """Where the runner gets its ``.sql`` files from.

    Two source types are supported:

      - ``local`` — point ``path`` at a directory of ``[0-9]{4}_*.sql`` files.
        Used for dev and tests.
      - ``git``   — shallow clone ``url`` at ``ref`` and read ``subpath`` from
        the resulting tree. ``token_env_var`` names the env var holding a token
        used to authenticate the clone (GitHub form:
        ``https://x-access-token:<token>@github.com/...``).
    """
    type: str = Field(default="git")  # "git" | "local"
    # git
    url: Optional[str] = None
    ref: str = Field(default="main")
    subpath: str = Field(default="migrations")
    token_env_var: Optional[str] = None
    # local
    path: Optional[str] = None


class MigrationTargetConfig(BaseModel):
    """One database the runner can manage.

    The layover supplies one of these per service it owns a database for.
    Lives under ``<Org>.migrations.<service_name>`` in config.yml.
    """
    db_url_env: str
    source: MigrationSourceConfig
    table_name: str = Field(default="schema_migrations")
    lock_timeout: str = Field(default="5s")
    max_retries: int = Field(default=5)
    # Cron drift-check cadence in seconds (the cron loop's sleep interval).
    check_interval_seconds: int = Field(default=900)
    # ``manual_apply`` — cron only emits drift events; applies go through the
    #                    admin endpoint. Safe default; what capacium-ops uses.
    # ``auto_apply``   — cron applies pending migrations automatically. Only
    #                    suitable for ephemeral envs (CI, throwaway dev).
    mode: str = Field(default="manual_apply")


# --- Aggregate Configs ---


class RepoConfig(BaseModel):
    stale_management: Optional[StaleManagementConfig] = None
    auto_triage: Optional[AutoTriageConfig] = None
    workflow_dispatches: list[WorkflowDispatchConfig] = Field(default_factory=list)
    dependency_triggers: list[DependencyTriggerConfig] = Field(default_factory=list)
    # The GitHub mirror name, only when it differs from the Forgejo repo name
    # (e.g. lvc-ops ``txt-humanizer`` -> ``txtHumanizer``). Declared so the
    # repo-level ``github_name`` config key is no longer dropped silently.
    github_name: str = Field(default="")
    # v2
    release: Optional[ReleaseConfig] = None
    auto_merge: Optional[MergeConfig] = None
    mirror: Optional[MirrorConfig] = None
    notifications: Optional[NotificationConfig] = None
    # v3.2 (DST-001): the canonical destination list. The forge is a value here,
    # not a key name. Deprecated mirror aliases resolve into this list.
    destinations: list[Destination] = Field(default_factory=list)

    def resolve_destinations(self) -> list[Destination]:
        """Resolve this repo's destinations into a :class:`Destination` list.

        The canonical list (``destinations``) is returned first, verbatim. Then
        the three DEPRECATED mirror aliases are consulted, each emitting a
        ``DeprecationWarning`` naming the removal version: ``mirror.github``
        (+``mirror.visibility``) and ``mirror_url`` (+``mirror.primary_forge``)
        each resolve into a single ``mirror``-role Destination, and an absent
        ``mirror`` section resolves to an empty list (the deliberate
        "unmirrored" case).
        """
        resolved: list[Destination] = list(self.destinations)

        if self.mirror is not None:
            mirror = self.mirror
            if mirror.github:
                warnings.warn(
                    "mirror.github is deprecated and will be removed in "
                    f"ops-engine {_DESTINATION_REMOVAL_VERSION}; use "
                    "destinations with forge/repo/role/visibility instead.",
                    DeprecationWarning,
                    stacklevel=2,
                )
                resolved.append(
                    Destination(
                        forge="github",
                        repo=mirror.github,
                        role="mirror",
                        visibility=mirror.visibility,
                    )
                )
            elif mirror.mirror_url:
                warnings.warn(
                    "mirror.mirror_url (with mirror.primary_forge) is deprecated "
                    f"and will be removed in ops-engine {_DESTINATION_REMOVAL_VERSION}; "
                    "use destinations with forge/repo/role/visibility instead.",
                    DeprecationWarning,
                    stacklevel=2,
                )
                forge = mirror.primary_forge or "forgejo"
                resolved.append(
                    Destination(
                        forge=forge,
                        repo=mirror.mirror_url,
                        role="mirror",
                        visibility=mirror.visibility,
                    )
                )

        return resolved


class ForgejoIdentity(BaseModel):
    """Forgejo-specific identity attributes for an org, stored under its key.

    ``display_name`` is the human-facing display name (free-form, can carry
    display case or a company name like ``"Lange Ventures & Consulting"``). It
    is a stored attribute — NEVER a lookup key. The canonical lookup key is the
    lowercase org handle (see :func:`canonical_org_key`).
    """

    display_name: Optional[str] = None


class GithubIdentity(BaseModel):
    """GitHub-specific identity attributes for an org, stored under its key.

    ``login`` is the GitHub account/login handle (can carry display case, e.g.
    ``LangeVC``). It is a stored attribute — NEVER a lookup key. The canonical
    lookup key is the lowercase org handle (see :func:`canonical_org_key`).
    """

    login: Optional[str] = None


class OrgConfig(BaseModel):
    # Forge identity attributes, stored under the canonical ``lower_name`` key.
    # These are data (attributes), never the key used to look a config up.
    forgejo: Optional[ForgejoIdentity] = None
    github: Optional[GithubIdentity] = None
    stale_management: StaleManagementConfig = Field(default_factory=StaleManagementConfig)
    auto_triage: AutoTriageConfig = Field(default_factory=AutoTriageConfig)
    # v2: org-level defaults for new configs
    release: Optional[ReleaseConfig] = None
    auto_merge: Optional[MergeConfig] = None
    notifications: Optional[NotificationConfig] = None
    repositories: dict[str, RepoConfig] = Field(default_factory=dict)
    # v2.1: schema-migration targets, keyed by service name. Org-only (a service
    # owns exactly one DB layout — there is no per-repo override.)
    migrations: dict[str, MigrationTargetConfig] = Field(default_factory=dict)


# The declared config sections and the model they must resolve to. Used to
# guarantee ``get_repo_config`` returns a typed model (never a raw dict) for
# every declared section, or raises ``ConfigSectionError`` naming the section.
_DECLARED_SECTIONS: dict[str, type[BaseModel]] = {
    "stale_management": StaleManagementConfig,
    "auto_triage": AutoTriageConfig,
    "workflow_dispatches": WorkflowDispatchConfig,
    "dependency_triggers": DependencyTriggerConfig,
    "release": ReleaseConfig,
    "auto_merge": MergeConfig,
    "mirror": MirrorConfig,
    "notifications": NotificationConfig,
}


def _section_error_from_validation(exc: ValidationError) -> ConfigSectionError:
    """Map a Pydantic ValidationError to a ConfigSectionError naming the section."""
    errors = exc.errors()
    if not errors:
        return ConfigSectionError("config", detail=str(exc))
    loc = list(errors[0].get("loc", ()))
    section = "config"
    org_name: Optional[str] = None
    repo_name: Optional[str] = None
    for i, part in enumerate(loc):
        if part == "orgs" and i + 1 < len(loc):
            org_name = str(loc[i + 1])
        elif part == "repositories" and i + 1 < len(loc):
            repo_name = str(loc[i + 1])
    for part in loc:
        if part in _DECLARED_SECTIONS:
            section = str(part)
            break
    return ConfigSectionError(
        section,
        org_name=org_name,
        repo_name=repo_name,
        detail=str(errors[0].get("msg")),
    )


def _assert_typed_sections(
    repo_config: RepoConfig,
    org_name: str,
    repo_name: str,
) -> None:
    """Ensure every non-null declared section is a typed model, not a raw dict."""
    for section, model_type in _DECLARED_SECTIONS.items():
        value = getattr(repo_config, section)
        if value is None:
            continue
        if isinstance(value, list):
            for item in value:
                if not isinstance(item, model_type):
                    raise ConfigSectionError(
                        section,
                        org_name=org_name,
                        repo_name=repo_name,
                        detail=(
                            f"expected {model_type.__name__}, "
                            f"got {type(item).__name__}"
                        ),
                    )
            continue
        if not isinstance(value, model_type):
            raise ConfigSectionError(
                section,
                org_name=org_name,
                repo_name=repo_name,
                detail=f"expected {model_type.__name__}, got {type(value).__name__}",
            )


class OpsEngineConfig(BaseModel):
    orgs: dict[str, OrgConfig] = Field(default_factory=dict)

    @classmethod
    def load(cls, data: dict[str, Any]) -> "OpsEngineConfig":
        """Build a typed config from a raw mapping (e.g. ``yaml.safe_load``).

        A raw dict is coerced into typed section models here; a malformed
        section raises ``ConfigSectionError`` naming the section rather than a
        bare ``ValidationError``.
        """
        try:
            return cls.model_validate(data)
        except ConfigSectionError:
            raise
        except ValidationError as exc:
            raise _section_error_from_validation(exc) from exc

    def get_repo_config(self, org_name: str, repo_name: str) -> RepoConfig:
        """Returns a resolved RepoConfig merging Org defaults with Repo specifics.

        ``org_name`` is expected to be the canonical org key — the lowercase
        org handle (see :func:`canonical_org_key`). Lookup
        is resolved against the stored org keys on that lowercase basis, so a
        caller that still holds a display-cased name resolves to the same config
        instead of missing it.

        Every declared section resolves to a typed model (never a raw dict).
        A missing org, or a section holding an untyped value, raises
        ``ConfigSectionError`` naming the offending section.
        """
        org_config = self.orgs.get(org_name)
        if org_config is None:
            org_config = next(
                (v for k, v in self.orgs.items() if k.lower() == org_name.lower()),
                None,
            )
        if org_config is None:
            raise ConfigSectionError(
                "orgs",
                org_name=org_name,
                repo_name=repo_name,
                detail=f"no org named {org_name!r}",
            )
        repo_specific = org_config.repositories.get(repo_name, RepoConfig())

        resolved = RepoConfig(
            stale_management=repo_specific.stale_management or org_config.stale_management,
            auto_triage=repo_specific.auto_triage or org_config.auto_triage,
            workflow_dispatches=repo_specific.workflow_dispatches,
            dependency_triggers=repo_specific.dependency_triggers,
            github_name=repo_specific.github_name,  # no org default (repo-specific only)
            # v2: merge with org defaults
            release=repo_specific.release or org_config.release,
            auto_merge=repo_specific.auto_merge or org_config.auto_merge,
            mirror=repo_specific.mirror,  # no org default (repo-specific only)
            notifications=repo_specific.notifications or org_config.notifications,
            destinations=repo_specific.destinations,  # no org default (repo-specific only)
        )
        _assert_typed_sections(resolved, org_name, repo_name)
        return resolved
