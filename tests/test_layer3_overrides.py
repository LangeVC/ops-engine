"""Layer 3: ``.ops.yaml`` exists, loads, and overrides the Layer-2 config.

DST-002. Layer 3 is a per-repository ``.ops.yaml`` that sits on top of the
org-layover Layer-2 ``config.yml``. Its precedence over Layer 2 is defined
here and sealed by ``test_layer3_list_replaces_layer2``, which fails if the
replace-vs-extend decision changes.
"""

import textwrap

import pytest

from ops_engine.config_loader import (
    OpsEngineConfig,
    OrgConfig,
    RepoConfig,
    Destination,
    OpsYamlError,
    load_ops_yaml,
)


def _layer2():
    return OpsEngineConfig(
        orgs={
            "langevc": OrgConfig(
                repositories={
                    "ops-engine": RepoConfig(
                        destinations=[
                            Destination(
                                forge="github",
                                repo="LangeVC/ops-engine",
                                role="mirror",
                                visibility="public",
                            )
                        ]
                    )
                }
            )
        }
    )


def _write(tmp_path, text):
    p = tmp_path / ".ops.yaml"
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return p


def test_absent_ops_yaml_returns_none(tmp_path):
    """AC3: an absent .ops.yaml is the normal case and changes nothing."""
    assert load_ops_yaml(tmp_path) is None


def test_layer3_merges_over_layer2(tmp_path):
    """AC1: a loader reads .ops.yaml and merges it over the Layer-2 config."""
    _write(tmp_path, """
        destinations:
          - forge: gitlab
            repo: langevc/ops-engine
            role: release
            visibility: private
    """)
    layer3 = load_ops_yaml(tmp_path)
    assert layer3 is not None

    base = _layer2().get_repo_config("langevc", "ops-engine")
    merged = base.merge_layer3(layer3)

    assert [d.forge for d in merged.destinations] == ["gitlab"]
    assert merged.destinations[0].role == "release"
    merged.resolve_destinations()


def test_layer3_list_replaces_layer2(tmp_path):
    """The precedence rule: a Layer-3 destinations list REPLACES the Layer-2 list.

    A list is the one value that could be argued to extend rather than replace.
    This test pins the decision: replacement. Why: a list is a source of truth
    for "the destinations this repo has", not an accumulator. Extension would
    make the resolved destination set the union of two files, which a reader
    cannot compute without both files in hand; replacement keeps every field
    exactly as its most-local declaration stated it. If this decision changes,
    this test fails.
    """
    _write(tmp_path, """
        destinations:
          - forge: local
            repo: /srv/git/ops-engine.git
            role: replica
    """)
    layer3 = load_ops_yaml(tmp_path)
    base = _layer2().get_repo_config("langevc", "ops-engine")
    merged = base.merge_layer3(layer3)
    resolved = merged.resolve_destinations()

    assert len(resolved) == 1, (
        "Layer-3 destinations must REPLACE Layer-2, not extend it; "
        f"got {len(resolved)} destinations"
    )
    assert resolved[0].forge == "local"


def test_malformed_ops_yaml_is_named_refusal(tmp_path):
    """AC4: a malformed .ops.yaml raises OpsYamlError naming the file."""
    path = _write(tmp_path, "{ not yaml: [")
    with pytest.raises(OpsYamlError) as exc:
        load_ops_yaml(tmp_path)
    assert str(path) in str(exc.value)


def test_malformed_ops_yaml_names_schema_error(tmp_path):
    """A well-formed YAML that violates the RepoConfig shape names the file too."""
    _write(tmp_path, "destinations: not-a-list\n")
    with pytest.raises(OpsYamlError) as exc:
        load_ops_yaml(tmp_path)
    assert ".ops.yaml" in str(exc.value)


def test_empty_ops_yaml_is_no_override(tmp_path):
    """An empty .ops.yaml is a valid no-op override."""
    _write(tmp_path, "")
    layer3 = load_ops_yaml(tmp_path)
    assert layer3 is not None
    base = _layer2().get_repo_config("langevc", "ops-engine")
    merged = base.merge_layer3(layer3)
    assert [d.forge for d in merged.resolve_destinations()] == ["github"]
