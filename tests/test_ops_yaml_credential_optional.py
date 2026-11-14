"""P36-020: credential_variable is optional with the forge default.

Criterion 3: The .ops.yaml of ai-engineering-ledger, faigate and skillweave
on their main branches load with engine 3.6.1 without edits.

Criterion 4 (RED PROOF): The same three fixtures are refused by the 3.6.0
loader (OpsYamlError) and load with the change.

The test carries the release-destination blocks as fixtures, calls
``load_ops_yaml`` through the real loader, and asserts the loaded
destinations resolve their credentials by forge key. On the base (3.6.0)
the validator ``_release_destinations_declare_credential_variable`` refuses
a release destination without ``credential_variable``, so this test FAILS
with ``OpsYamlError`` — the RED proof.
"""

import pytest
import yaml

from ops_engine.config_loader import Destination, RepoConfig, load_ops_yaml

# The release-destination blocks of the three repositories on their main
# branches as of 2026-11-14. None declares credential_variable, and the
# 3.6.0 loader refuses them for that reason (OpsYamlError). The blocks
# carry exactly the fields the real files carry; the files also carry
# comments, forge_hosts and tracker_prefixes, which the loader ignores
# for destination resolution.

AI_ENGINEERING_LEDGER = yaml.safe_load("""
destinations:
  - forge: forgejo
    repo: langevc/ai-engineering-ledger
    role: release
    visibility: private
  - forge: github
    repo: LangeVC/ai-engineering-ledger
    role: release
    visibility: public
""")

FAIGATE = yaml.safe_load("""
destinations:
  - forge: forgejo
    repo: fusionaize/faigate
    role: release
    visibility: private
  - forge: github
    repo: fusionAIze/faigate
    role: release
    visibility: public
""")

SKILLWEAVE = yaml.safe_load("""
destinations:
  - forge: forgejo
    repo: skillweave/skillweave
    role: release
    visibility: public
  - forge: github
    repo: SkillWeave-AI/skillweave
    role: release
    visibility: public
""")


def _load_yaml_as_repo_config(data: dict) -> RepoConfig:
    """Load a dict as if it were the content of a .ops.yaml file.

    ``load_ops_yaml`` reads the file from disk; this helper passes the
    dict through the same ``RepoConfig.model_validate`` path with the
    ``.ops.yaml`` context so the per-layer validators see it.
    """
    return RepoConfig.model_validate(
        data,
        context={"source": ".ops.yaml"},
    )


def test_ai_engineering_ledger_loads_without_credential_variable():
    """Criterion 3/4: ai-engineering-ledger's release destinations load.

    On the 3.6.0 base this raises ``OpsYamlError`` because the validator
    refuses a release destination without ``credential_variable``. After
    the fix, the destinations load and each resolves its credential by
    its forge key.
    """
    cfg = _load_yaml_as_repo_config(AI_ENGINEERING_LEDGER)
    release_dests = [d for d in cfg.destinations if d.role == "release"]
    assert len(release_dests) == 2

    forgejo = next(d for d in release_dests if d.forge == "forgejo")
    github = next(d for d in release_dests if d.forge == "github")
    assert forgejo.repo == "langevc/ai-engineering-ledger"
    assert github.repo == "LangeVC/ai-engineering-ledger"
    assert forgejo.credential_variable == ""
    assert github.credential_variable == ""


def test_faigate_loads_without_credential_variable():
    """Criterion 3/4: faigate's release destinations load.

    On the 3.6.0 base this raises ``OpsYamlError``. After the fix the
    destinations load.
    """
    cfg = _load_yaml_as_repo_config(FAIGATE)
    release_dests = [d for d in cfg.destinations if d.role == "release"]
    assert len(release_dests) == 2

    forgejo = next(d for d in release_dests if d.forge == "forgejo")
    github = next(d for d in release_dests if d.forge == "github")
    assert forgejo.repo == "fusionaize/faigate"
    assert github.repo == "fusionAIze/faigate"
    assert forgejo.credential_variable == ""
    assert github.credential_variable == ""


def test_skillweave_loads_without_credential_variable():
    """Criterion 3/4: skillweave's release destinations load.

    On the 3.6.0 base this raises ``OpsYamlError``. After the fix the
    destinations load.
    """
    cfg = _load_yaml_as_repo_config(SKILLWEAVE)
    release_dests = [d for d in cfg.destinations if d.role == "release"]
    assert len(release_dests) == 2

    forgejo = next(d for d in release_dests if d.forge == "forgejo")
    github = next(d for d in release_dests if d.forge == "github")
    assert forgejo.repo == "skillweave/skillweave"
    assert github.repo == "SkillWeave-AI/skillweave"
    assert forgejo.credential_variable == ""
    assert github.credential_variable == ""


def test_credential_variable_still_wins_when_declared():
    """Criterion 1: A destination that does declare credential_variable
    still uses it; the forge fallback does not override a declared variable.
    """
    data = yaml.safe_load("""
    destinations:
      - forge: github
        repo: org/repo
        role: release
        credential_variable: MY_GH_TOKEN
    """)
    cfg = _load_yaml_as_repo_config(data)
    dest = cfg.destinations[0]
    assert dest.credential_variable == "MY_GH_TOKEN"
    assert dest.forge == "github"


def test_two_github_destinations_with_different_variables_still_receive_different_tokens():
    """Criterion 1: Two destinations of one forge with different variables
    still receive different tokens.

    This exercises ``adapters_for`` through the factory: the first
    destination resolves by its own variable, the second by its own, and
    they must be different.
    """
    from ops_engine.adapters.factory import Credential, adapters_for

    dest_a = Destination(
        forge="github", repo="org/repo-a", role="release",
        credential_variable="GH_TOKEN_A",
    )
    dest_b = Destination(
        forge="github", repo="org/repo-b", role="release",
        credential_variable="GH_TOKEN_B",
    )

    cred_a = Credential(token="token-alpha", webhook_secret="sa")
    cred_b = Credential(token="token-beta", webhook_secret="sb")

    adapters = adapters_for(
        [dest_a, dest_b],
        credentials={"GH_TOKEN_A": cred_a, "GH_TOKEN_B": cred_b},
        base_url="https://api.github.com",
    )

    assert len(adapters) == 2
    assert adapters[0].token == "token-alpha"
    assert adapters[1].token == "token-beta"
    assert adapters[0].token != adapters[1].token


def test_destination_without_credential_variable_resolves_by_forge_key():
    """Criterion 1: A release destination without credential_variable
    resolves its credential by the forge key through ``adapters_for``.
    """
    from ops_engine.adapters.factory import Credential, adapters_for

    dest = Destination(
        forge="github", repo="org/repo", role="release",
    )
    assert dest.credential_variable == ""

    cred = Credential(token="github-token", webhook_secret="s")

    adapters = adapters_for(
        [dest],
        credentials={"github": cred},
        base_url="https://api.github.com",
    )

    assert len(adapters) == 1
    assert adapters[0].token == "github-token"


def test_missing_credential_by_forge_key_fails_by_name():
    """Criterion 2: A missing credential for the resolved key (forge fallback)
    still fails that destination by name and leaves the others running.
    """
    from ops_engine.adapters.factory import Credential, MissingCredentialError, adapters_for

    dest_forgejo = Destination(
        forge="forgejo", repo="org/engine", role="release",
        credential_variable="FORGEJO_TOKEN",
    )
    dest_github = Destination(
        forge="github", repo="org/engine", role="release",
    )

    cred_forgejo = Credential(token="fj-token", webhook_secret="s")

    # The GitHub destination has no credential_variable, so it falls back
    # to the forge key "github". That key is missing from the mapping.
    with pytest.raises(MissingCredentialError) as exc_info:
        adapters_for(
            [dest_forgejo, dest_github],
            credentials={"FORGEJO_TOKEN": cred_forgejo},
            base_url="https://code.example.org",
        )

    message = str(exc_info.value)
    assert "org/engine" in message, (
        f"error must name the destination repo; got: {message}"
    )
    assert "github" in message, (
        f"error must name the forge; got: {message}"
    )
