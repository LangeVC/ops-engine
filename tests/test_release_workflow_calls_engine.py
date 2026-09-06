"""ADP-004: the release workflow calls the engine instead of reimplementing it.

The four curl steps that used to POST each forge's release API are gone; the
workflow now builds the four assets, runs the five gates, and hands publication
to ``ReleaseHandler.publish_release``. The three acceptance criteria:

1. No forge repository or API host appears as a literal in ``.forgejo/``; the
   destination reaches the engine from config.
2. The five preserved behaviours still fail the release when violated (title
   gate, changelog notes, audience gate, OSV gate, reproducibility) — each of
   these is a separate gate whose tests live in the other release test files;
   this file re-asserts the ordering and the engine-call seam.
3. A real tag push publishes to both forges — that is an operator decision and
   lives outside the test suite (a real release, not a fake run).

This file proves the SEAM the workflow now depends on: that the publish block
the workflow runs is exactly the engine call, that the destination is resolved
from config (not a literal), and that the literal-laden publication path is
gone.
"""

import textwrap
from pathlib import Path

import yaml

from ops_engine.config_loader import OpsEngineConfig
from ops_engine.modules.mirror import resolve_destinations
from ops_engine.modules.release import render_release_name

REPO_ROOT = Path(__file__).resolve().parents[1]
FORGEJO = REPO_ROOT / ".forgejo"
FORGEJO_RELEASE = FORGEJO / "workflows" / "forgejo-release.yml"

WORKFLOW_CONTENT = FORGEJO_RELEASE.read_text(encoding="utf-8")


def _publish_block() -> str:
    """The engine-call Python the workflow runs, de-indented (PUBLISH_PY block)."""
    lines = WORKFLOW_CONTENT.splitlines()
    start = end = None
    for i, line in enumerate(lines):
        if "<<'PUBLISH_PY'" in line:
            start = i
        if start is not None and line.strip() == "PUBLISH_PY" and i > start:
            end = i
            break
    assert start is not None and end is not None, "PUBLISH_PY block not found"
    return textwrap.dedent("\n".join(lines[start + 1 : end]) + "\n")


# --- Criterion 1 prompt-face: the destination comes from config, not a literal


def _forge_destination_literals() -> list[str]:
    """Tokens that would name a forge repository or API host as a literal."""
    return [
        "api.github.com",
        "uploads.github.com",
        "LangeVC/ops-engine",
        "GH_REPO=",
        "GH_API=",
    ]


def test_no_forge_destination_literal_in_the_release_workflow():
    """None of the destination / API-host literals the old release workflow
    hardcoded survive in ``.forgejo/workflows/forgejo-release.yml`` — the
    release path's destination now reaches the engine from config.

    (mirror.yml's git-ref push URL is the git mirror, a separate surface owned
    by the mirror-destination lane; the release destination is what ADP-004
    moves into config.)
    """
    offending: list[int] = []
    for lit in _forge_destination_literals():
        for lineno, line in enumerate(WORKFLOW_CONTENT.splitlines(), start=1):
            if lit in line:
                offending.append(lineno)
    assert not offending, f"destination literals still present at lines: {offending}"


def test_publish_block_resolves_destinations_from_config():
    """The publish block reads the committed .ops.yaml via load_ops_yaml and
    materialises a typed OpsEngineConfig from it; no Actions variable and no
    destination literal survive in the workflow layer (ADP-008)."""
    block = _publish_block()
    assert "load_ops_yaml" in block
    assert "RELEASE_DESTINATIONS" not in block
    assert "OpsEngineConfig.load" in block
    assert "github.repository" in WORKFLOW_CONTENT or "FORGEJO_REPOSITORY" in block


def test_publish_block_calls_the_engine():
    """The publish block invokes ReleaseHandler.publish_release — it does not
    emit any curl/HTTP construction of its own."""
    block = _publish_block()
    assert "ReleaseHandler.publish_release" in block
    # No hand-rolled release HTTP: the block constructs adapters and delegates.
    assert "curl" not in block
    assert "httpx" not in block


def test_name_template_renders_the_gated_convention():
    """The workflow's name_template renders the same name the title gate
    validated: 'Ops Engine {tag_name}' against the tag yields exactly the
    gated convention 'Ops Engine v3.2.0'."""
    assert "Ops Engine {tag_name}" in WORKFLOW_CONTENT
    name = render_release_name("Ops Engine {tag_name}", "langevc/ops-engine", "v3.2.0")
    assert name == "Ops Engine v3.2.0"


# --- Criterion 2 seam: the gates still run before the engine publishes -------


def test_osv_gate_runs_before_the_engine_call():
    """The OSV gate still precedes the release-creation step, so a failing scan
    aborts before any release object exists on either forge."""
    gate_idx = WORKFLOW_CONTENT.index("Gate the SBOM against OSV")
    create_idx = WORKFLOW_CONTENT.index("name: Create Release")
    assert gate_idx < create_idx


def test_all_five_gates_precede_the_engine_call():
    """Every gate named by criterion 2 precedes the Create Release step, so a
    violation fails the run before publication."""
    gates = [
        "Gate the release title",
        "Extract release notes from CHANGELOG",
        "Gate the release notes for an external audience",
        "Build reproducible assets",
        "Gate the SBOM against OSV",
    ]
    create_idx = WORKFLOW_CONTENT.index("name: Create Release")
    for gate in gates:
        assert WORKFLOW_CONTENT.index(gate) < create_idx, gate


def test_publish_block_constructs_one_adapter_per_destination():
    """The publish block builds a ForgejoAdapter for the canonical forge and a
    GithubAdapter for each github destination — two forges, two adapters, no
    shared credential."""
    block = _publish_block()
    assert "ForgejoAdapter" in block
    assert "GithubAdapter" in block
    # The two credentials are distinct tokens, not one shared one.
    assert "FORGEJO_TOKEN" in block
    assert "GH_MIRROR_TOKEN" in block


# --- The destination-to-adapter wiring actually runs (real execution) ---------


def test_config_from_the_workflow_shape_resolves_two_destinations():
    """Reconstruct the config the workflow builds and prove the engine resolves
    exactly the canonical Forgejo destination plus the configured mirrors — a
    real run of resolve_destinations, not a text read."""
    raw_destinations = yaml.safe_load(
        "- {forge: github, repo: LangeVC/ops-engine, role: release}\n"
    )
    repo = "langevc/ops-engine"
    org, _, repo_name = repo.partition("/")
    config = OpsEngineConfig.load(
        {
            "orgs": {
                org: {
                    "repositories": {
                        repo_name: {
                            "destinations": [
                                {"forge": "forgejo", "repo": repo, "role": "release"}
                            ]
                            + raw_destinations
                        }
                    }
                }
            }
        }
    )
    destinations = resolve_destinations(config, repo)
    assert [(d.forge, d.repo) for d in destinations] == [
        ("forgejo", "langevc/ops-engine"),
        ("github", "LangeVC/ops-engine"),
    ]
