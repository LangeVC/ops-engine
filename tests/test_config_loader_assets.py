"""REA-010: the release block's optional ``assets`` declaration.

The lane criteria, verbatim:

1. A .ops.yaml with an assets block loads, merges over layer 2 and is visible
   in the resolved config.
2. An unknown or malformed assets entry is refused with a message naming the
   key and the file.
3. RED PROOF: against 2f5c255 a config with assets: loads without the field
   (or is dropped); the new loader returns it.

The declarations below are the shapes the fleet actually writes in the
layover ``config.yml``: ``assets: none`` on a repository with no required
assets, a list naming the built artifacts, and a ``capacium_exchange`` entry
published through a workflow step.
"""

import textwrap

import pytest

from ops_engine.config_loader import (
    OpsEngineConfig,
    OrgConfig,
    RepoConfig,
    Destination,
    OpsYamlError,
    ReleaseConfig,
    load_ops_yaml,
)


def _layer2(release=None):
    """A minimal Layer-2 config: one org, one repo, a public mirror destination."""
    return OpsEngineConfig(
        orgs={
            "langevc": OrgConfig(
                repositories={
                    "ops-engine": RepoConfig(
                        visibility_candidate_globs=["docs/private/**"],
                        destinations=[
                            Destination(
                                forge="github",
                                repo="LangeVC/ops-engine",
                                role="mirror",
                                visibility="public",
                            )
                        ],
                        release=release,
                    )
                }
            )
        }
    )


def _write(tmp_path, text):
    path = tmp_path / ".ops.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def _resolved_and_merged(tmp_path, text, release=None):
    """Load the .ops.yaml, merge it over the resolved Layer-2 config."""
    _write(tmp_path, text)
    layer3 = load_ops_yaml(tmp_path)
    assert layer3 is not None
    resolved = _layer2(release=release).get_repo_config("langevc", "ops-engine")
    return layer3, resolved.merge_layer3(layer3)


FLEET_ASSETS = """
    release:
      enabled: true
      assets:
        - type: github_release_tarball
          name: "txtHumanizer-v<version>.tar.gz"
        - type: capacium_exchange
          via: "Capacium/capacium-action-publish@v1"
        - type: ghcr_image
          name: "ghcr.io/langevc/agent-test-env-<framework>"
          frameworks: [opencode, claude-code, codex-cli, gemini-cli, continue]
      note: "No GitHub Release asset; images only."
"""


# --- Criterion 1: loads, merges over Layer 2, visible in the resolved config --


def test_layer3_assets_load_merge_and_are_visible_in_the_resolved_config(tmp_path):
    """AC1: a .ops.yaml assets block survives the merge into the resolved config."""
    _write(tmp_path, FLEET_ASSETS)
    layer3 = load_ops_yaml(tmp_path)
    assert layer3 is not None
    assert [a.type for a in layer3.release.assets] == [
        "github_release_tarball",
        "capacium_exchange",
        "ghcr_image",
    ]

    resolved = _layer2(release=ReleaseConfig(enabled=True)).get_repo_config(
        "langevc", "ops-engine"
    )
    merged = resolved.merge_layer3(layer3)

    assert merged.release is not None
    assert merged.release.enabled is True
    assets = merged.release.assets
    assert assets is not None
    assert [a.type for a in assets] == [
        "github_release_tarball",
        "capacium_exchange",
        "ghcr_image",
    ]
    assert assets[0].name == "txtHumanizer-v<version>.tar.gz"
    assert assets[1].via == "Capacium/capacium-action-publish@v1"
    assert assets[2].frameworks == [
        "opencode",
        "claude-code",
        "codex-cli",
        "gemini-cli",
        "continue",
    ]


def test_layer2_assets_are_visible_in_the_resolved_config():
    """AC1: a layover release block declaring assets reaches get_repo_config."""
    config = OpsEngineConfig(
        orgs={
            "langevc": OrgConfig(
                repositories={
                    "wp-test-env": RepoConfig(
                        release={
                            "enabled": True,
                            "assets": [
                                {
                                    "type": "github_release_zip",
                                    "name": "wp-test-env-<version>.zip",
                                },
                                {
                                    "type": "ghcr_image",
                                    "name": "ghcr.io/langevc/wp-test-env:<tag>",
                                },
                            ],
                        },
                    )
                }
            )
        }
    )

    resolved = config.get_repo_config("langevc", "wp-test-env")
    assert [a.type for a in resolved.release.assets] == [
        "github_release_zip",
        "ghcr_image",
    ]


def test_layer3_assets_replace_layer2_assets(tmp_path):
    """AC1: the release block is one value; Layer 3 replaces it whole."""
    layer3, merged = _resolved_and_merged(
        tmp_path,
        """
        release:
          enabled: true
          assets:
            - type: ghcr_image
              name: "ghcr.io/langevc/replacement:<tag>"
        """,
        release={
            "enabled": True,
            "assets": [{"type": "github_release_zip", "name": "layer2.zip"}],
        },
    )
    assert layer3.release.assets is not None
    assert [a.name for a in merged.release.assets] == [
        "ghcr.io/langevc/replacement:<tag>"
    ]


def test_layer3_assets_none_clears_the_layer2_declaration(tmp_path):
    """AC1: ``assets: none`` is an explicit override, not an absent key."""
    _, merged = _resolved_and_merged(
        tmp_path,
        """
        release:
          assets: none
        """,
        release={
            "enabled": True,
            "assets": [{"type": "github_release_zip", "name": "layer2.zip"}],
        },
    )
    assert merged.release.assets is None


# --- Absent / explicit none mean "no required assets" ------------------------


def test_absent_assets_means_no_required_assets():
    """The key is optional: without it the release requires no assets."""
    assert "assets" not in ReleaseConfig.model_fields
    assert ReleaseConfig(enabled=True).assets is None


def test_assets_none_and_null_are_no_required_assets(tmp_path):
    """The fleet writes bare ``assets: none``; ``~``/null is the same."""
    for body in (
        """
        release:
          enabled: true
          assets: none
          name_template: "Ops Engine {tag_name}"
        """,
        """
        release:
          enabled: true
          assets: ~
          name_template: "Ops Engine {tag_name}"
        """,
    ):
        _write(tmp_path, body)
        layer3 = load_ops_yaml(tmp_path)
        assert layer3 is not None
        assert layer3.release.assets is None
        assert layer3.release.name_template == "Ops Engine {tag_name}"
        assert layer3.release.enabled is True


# --- Criterion 2: unknown or malformed entries are refused by key and file ---


def _refusal(tmp_path, body):
    path = _write(tmp_path, body)
    with pytest.raises(OpsYamlError) as exc:
        load_ops_yaml(tmp_path)
    message = str(exc.value)
    assert str(path) in message, message
    assert "release.assets" in message, message
    return message


def test_unknown_asset_type_is_refused_naming_key_and_file(tmp_path):
    """AC2: an unknown type is refused, naming release.assets and the .ops.yaml."""
    message = _refusal(
        tmp_path,
        """
        release:
          assets:
            - type: mystery_asset
              name: "whatever"
        """,
    )
    assert "mystery_asset" in message


def test_asset_missing_required_name_is_refused_naming_key_and_file(tmp_path):
    """AC2: a declaration without the name its type requires is refused."""
    message = _refusal(
        tmp_path,
        """
        release:
          assets:
            - type: github_release_zip
        """,
    )
    assert "missing 'name'" in message


def test_capacium_exchange_missing_via_is_refused_naming_key_and_file(tmp_path):
    """AC2: ``capacium_exchange`` requires the workflow step that publishes it."""
    message = _refusal(
        tmp_path,
        """
        release:
          assets:
            - type: capacium_exchange
        """,
    )
    assert "missing 'via'" in message


def test_unknown_key_inside_an_asset_entry_is_refused(tmp_path):
    """AC2: a typo inside a declaration is refused, never carried along."""
    message = _refusal(
        tmp_path,
        """
        release:
          assets:
            - type: github_release_zip
              name: "built.zip"
              nmae: "typo"
        """,
    )
    assert "github_release_zip" in message


def test_assets_that_is_not_a_list_is_refused_naming_key_and_file(tmp_path):
    """AC2: ``assets`` accepts a list or the string 'none' — nothing else."""
    message = _refusal(
        tmp_path,
        """
        release:
          assets: not-a-list
        """,
    )
    assert "not-a-list" in message or "str" in message


def test_layer2_unknown_asset_type_is_refused_naming_key():
    """AC2: a layover config is refused too, naming the release.assets key."""
    from ops_engine.config_loader import ConfigSectionError

    with pytest.raises(ConfigSectionError) as exc:
        OpsEngineConfig.load(
            {
                "orgs": {
                    "langevc": {
                        "repositories": {
                            "ops-engine": {
                                "release": {
                                    "assets": [{"type": "mystery_asset"}],
                                }
                            }
                        }
                    }
                }
            }
        )
    assert "release.assets" in str(exc.value)


# --- Criterion 3: the red proof ---------------------------------------------


def test_red_proof_base_dropped_assets_and_the_loader_returns_them(tmp_path):
    """AC3: at 2f5c255 the key was a silently-dropped extra.

    This test fails against the base commit in two ways: the parsed model has
    no ``assets`` accessor (the key was dropped by pydantic's default
    ``extra='ignore'``), and an unknown type loads without a refusal. With the
    loader in place both halves pass.
    """
    _, merged = _resolved_and_merged(
        tmp_path,
        FLEET_ASSETS,
        release=ReleaseConfig(enabled=True),
    )
    assert merged.release.assets is not None
    assert len(merged.release.assets) == 3

    with pytest.raises(OpsYamlError):
        _write(
            tmp_path,
            """
            release:
              assets:
                - type: mystery_asset
            """,
        )
        load_ops_yaml(tmp_path)


# --- The contract statement --------------------------------------------------


def test_contract_describes_the_release_assets_key():
    """The key is described in CONTRACT.md, as the lane requires."""
    from pathlib import Path

    contract = (Path(__file__).resolve().parents[1] / "CONTRACT.md").read_text(
        encoding="utf-8"
    )
    section = contract.split("## Release assets declaration", 1)
    assert len(section) == 2, "CONTRACT.md has no release assets declaration"
    body = section[1].split("\n## ", 1)[0]
    assert "`release.assets`" in body
    assert "OpsYamlError" in body
    assert "minor" in body
