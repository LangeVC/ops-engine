"""FFR-200-3: every repo reference resolves through the canonical org key.

A repo reference is any string in a layover ``config.yml`` that names a
repository: an ``orgs`` key, a ``repositories`` key, or a
``dependency_triggers[].target_repo`` full name (``org/repo``). Every such
reference must resolve through the canonical org key — the Forgejo
``lower_name``, always lowercase — never through ``github.login`` or
``forgejo.display_name``. A configured target whose org is not a known
canonical org fails the config resolution (``ConfigSectionError``) before any
dispatch is issued.

The org set and reference surfaces are declared in
``docs/org-identifier-sweep.md`` (machine-readable JSON), mirroring the
FFR-100-3 declaration pattern.
"""

import json
import re
from pathlib import Path

import pytest

from ops_engine.config_loader import (
    ConfigSectionError,
    DependencyTriggerConfig,
    ForgejoIdentity,
    GithubIdentity,
    OpsEngineConfig,
    OrgConfig,
    RepoConfig,
    canonical_org_key,
)

SWEEP_PATH = Path(__file__).resolve().parent.parent / "docs" / "org-identifier-sweep.md"

EXPECTED_SURFACES = [
    {"field": "orgs", "kind": "canonical-key"},
    {"field": "repositories", "kind": "repo-name"},
    {"field": "dependency_triggers[].target_repo", "kind": "org/repo"},
]


def _load_declaration():
    text = SWEEP_PATH.read_text(encoding="utf-8")
    match = re.search(r"```json\n(.*?)\n```", text, re.DOTALL)
    assert match, "docs/org-identifier-sweep.md must contain a ```json``` declaration block"
    return json.loads(match.group(1))


def _config_from_declaration(declaration):
    orgs = {}
    for org in declaration["orgs"]:
        targets = [DependencyTriggerConfig(target_repo=t) for t in org.get("targets", [])]
        repositories = {repo: RepoConfig(dependency_triggers=list(targets)) for repo in org["repos"]}
        orgs[org["canonical_key"]] = OrgConfig(
            forgejo=ForgejoIdentity(display_name=org.get("forgejo_display_name")),
            github=GithubIdentity(login=org.get("github_login")),
            repositories=repositories,
        )
    return OpsEngineConfig(orgs=orgs)


def _canonical_key(config, org_name):
    """The canonical key for ``org_name`` (case-folded, like ``get_repo_config``)."""
    if org_name in config.orgs:
        return org_name
    for key in config.orgs:
        if key.lower() == org_name.lower():
            return key
    return None


def _resolve_repo_reference(config, reference):
    """The canonical key path for an ``org/repo`` reference.

    The org portion must resolve to a known canonical org — an unknown org
    raises ``ConfigSectionError`` here, i.e. at config resolution and before
    any dispatch.
    """
    org, separator, repo = reference.partition("/")
    if not separator or not org or not repo:
        raise ConfigSectionError("orgs", org_name=reference, detail="not an 'org/repo' reference")
    config.get_repo_config(org, repo)
    key = _canonical_key(config, org)
    return f"{key}/{repo}"


class RecordingAdapter:
    """Fake forge adapter that records every dispatch, so a test can prove that
    an unresolvable target never reaches the forge."""

    def __init__(self):
        self.dispatched = []

    def dispatch_workflow(self, repo_full_name, event_type, client_payload=None):
        self.dispatched.append((repo_full_name, event_type, client_payload))


def _resolve_then_dispatch(config, source_org, source_repo, adapter):
    """Reference flow: resolve the source repo, resolve every trigger target
    through the canonical key path, and only then dispatch. A target whose org
    is unknown raises ``ConfigSectionError`` during resolution, so the adapter
    never sees a dispatch for it."""
    resolved = config.get_repo_config(source_org, source_repo)
    targets = [_resolve_repo_reference(config, t.target_repo) for t in resolved.dependency_triggers]
    for target in targets:
        adapter.dispatch_workflow(target, "dependency-update")


# ── Declaration integrity (docs/org-identifier-sweep.md) ──────────────────────


def test_sweep_declares_schema_and_rule():
    declaration = _load_declaration()
    assert declaration.get("schema") == 1
    assert declaration.get("package") == "ops_engine"
    assert declaration.get("rule")


def test_sweep_declares_all_repo_reference_surfaces():
    declaration = _load_declaration()
    assert declaration.get("reference_surfaces") == EXPECTED_SURFACES


def test_sweep_orgs_carry_only_canonical_keys():
    declaration = _load_declaration()
    keys = [org["canonical_key"] for org in declaration["orgs"]]
    assert all(key == key.lower() for key in keys)
    assert len(set(keys)) == len(keys)


# ── Criterion 1: every repo reference resolves through the canonical key path ──


def test_every_declared_repo_reference_resolves_through_canonical_key_path():
    declaration = _load_declaration()
    config = _config_from_declaration(declaration)
    for org in declaration["orgs"]:
        key = org["canonical_key"]
        expected = [DependencyTriggerConfig(target_repo=t) for t in org.get("targets", [])]
        for repo in org["repos"]:
            resolved = config.get_repo_config(key, repo)
            assert resolved.dependency_triggers == expected
        for target in org.get("targets", []):
            assert _resolve_repo_reference(config, target) == target


def test_display_cased_repo_reference_resolves_to_canonical_key():
    declaration = _load_declaration()
    config = _config_from_declaration(declaration)
    org = next(o for o in declaration["orgs"] if o["canonical_key"] == "langevc")
    repo = org["repos"][0]
    expected = [DependencyTriggerConfig(target_repo=t) for t in org["targets"]]
    assert config.get_repo_config("LangeVC", repo).dependency_triggers == expected
    assert _resolve_repo_reference(config, f"LangeVC/{repo}") == f"{org['canonical_key']}/{repo}"


def test_repo_reference_resolves_from_forgejo_lower_name_not_full_name():
    repository = {
        "owner": {
            "id": 1,
            "login": "LangeVC",
            "full_name": "Lange Ventures & Consulting",
            "username": "langevc",
            "lower_name": "langevc",
        },
        "name": "ops-engine",
        "full_name": "Lange Ventures & Consulting/ops-engine",
    }
    assert canonical_org_key(repository) == "langevc"
    config = _config_from_declaration(_load_declaration())
    resolved = config.get_repo_config("langevc", "ops-engine")
    assert resolved.dependency_triggers == [DependencyTriggerConfig(target_repo="fusionaize/faigrid")]


def test_github_login_and_display_name_are_data_never_keys():
    config = _config_from_declaration(_load_declaration())
    assert set(config.orgs) == {"langevc", "fusionaize"}
    assert "LangeVC" not in config.orgs
    assert "Lange Ventures & Consulting" not in config.orgs
    with pytest.raises(ConfigSectionError) as excinfo:
        config.get_repo_config("Lange Ventures & Consulting", "ops-engine")
    assert excinfo.value.section == "orgs"
    assert excinfo.value.org_name == "Lange Ventures & Consulting"


# ── Criterion 2: an unknown org fails the config resolution, not the dispatch ──


def test_unknown_org_target_fails_config_resolution_not_dispatch():
    declaration = _load_declaration()
    config = _config_from_declaration(declaration)
    config.orgs["langevc"].repositories["ops-engine"].dependency_triggers.append(
        DependencyTriggerConfig(target_repo="ghostorg/legacy-service")
    )
    adapter = RecordingAdapter()
    with pytest.raises(ConfigSectionError) as excinfo:
        _resolve_then_dispatch(config, "langevc", "ops-engine", adapter)
    assert excinfo.value.section == "orgs"
    assert excinfo.value.org_name == "ghostorg"
    assert adapter.dispatched == []


def test_unknown_org_failure_is_named_config_error_not_adapter_error():
    config = _config_from_declaration(_load_declaration())
    with pytest.raises(ConfigSectionError) as excinfo:
        config.get_repo_config("ghostorg", "legacy-service")
    assert excinfo.value.section == "orgs"
    assert excinfo.value.org_name == "ghostorg"
    assert excinfo.value.repo_name == "legacy-service"


# ── Criterion 3: all five layover configs are swept, findings listed per file ──

# The five production org-layover ``config.yml`` files (checked out under
# ``langevc/`` on the Forgejo canonical host). The sweep must enumerate all of
# them so the per-file findings can never silently leave one config out.
EXPECTED_LAYOVERS = [
    "lvc-ops",
    "capacium-ops",
    "elementeer-ops",
    "fusionaize-ops",
    "skillweave-ops",
]

# Findings recorded for each config after the 2026-08-23 sweep. A canonical-org
# top-level key is fine by itself; a non-canonical key or a display-cased
# ``target_repo`` org portion is a finding. ``canonical_org_key`` asserts the
# declared key is exactly its Forgejo ``lower_name``.
EXPECTED_SWEEP = {
    "lvc-ops": {
        "declared_org_key": "LangeVC",
        "canonical_org_key": "langevc",
        "findings": [],
    },
    "capacium-ops": {
        "declared_org_key": "Capacium",
        "canonical_org_key": "capacium",
        "findings": [
            "Capacium/homebrew-tap-capacium",
            "Capacium/capacium-action-validate",
            "Capacium/capacium-exchange",
            "Capacium/capacium-crawler",
            "Capacium/capacium",
            "Capacium/capacium-models",
        ],
    },
    "elementeer-ops": {
        "declared_org_key": "elementeer",
        "canonical_org_key": "elementeer",
        "findings": [],
    },
    "fusionaize-ops": {
        "declared_org_key": "fusionaize",
        "canonical_org_key": "fusionaize",
        "findings": [
            "fusionAIze/faiops-browser",
            "fusionAIze/faiops-cli",
            "fusionAIze/faios",
            "fusionAIze/fusionaize-sdk",
            "fusionAIze/homebrew-tap",
        ],
    },
    "skillweave-ops": {
        "declared_org_key": "SkillWeave",
        "canonical_org_key": "skillweave",
        "findings": [],
    },
}


def _load_sweep():
    text = SWEEP_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)
    assert len(blocks) >= 2, (
        "docs/org-identifier-sweep.md must contain the reference declaration and "
        "the five-layover sweep declaration"
    )
    return json.loads(blocks[1])


def test_sweep_covers_all_five_layover_configs():
    declaration = _load_sweep()
    layovers = [entry["config"] for entry in declaration["layovers"]]
    assert sorted(layovers) == sorted(EXPECTED_LAYOVERS)


def test_sweep_lists_per_file_findings():
    declaration = _load_sweep()
    for entry in declaration["layovers"]:
        name = entry["config"]
        expected = EXPECTED_SWEEP[name]
        assert entry["declared_org_key"] == expected["declared_org_key"]
        assert entry["canonical_org_key"] == expected["canonical_org_key"]
        assert entry["findings"] == expected["findings"]


def test_sweep_findings_are_repo_references_not_display_data():
    declaration = _load_sweep()
    for entry in declaration["layovers"]:
        canonical = entry["canonical_org_key"]
        assert canonical == canonical.lower(), f"{canonical!r} is not lowercase"
        for finding in entry["findings"]:
            org, separator, repo = finding.partition("/")
            assert separator and org and repo, f"{finding!r} is not an 'org/repo' reference"
            assert org != canonical, (
                f"{finding!r} already references the canonical org; it is not a finding"
            )
            assert org.lower() == canonical, (
                f"{org!r} does not lower to the canonical org {canonical!r}"
            )
        assert entry["declared_org_key"].lower() == canonical, (
            f"{entry['config']} declared key does not lower to its canonical key"
        )
