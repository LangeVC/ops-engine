"""OME-008: the visibility classifier judges what a ref CARRIES, by content.

These tests exercise every code branch of the classifier with synthetic content
(names and structure only — no real planning prose). The content/substrate split
is proven against real repository content in the verdict: the same path
``tests/fixtures/prd-schema/forgejo-first.json`` classifies SUBSTRATE when its
prose survives on a non-default branch, and PRODUCT when redacted to the neutral
placeholder on ``main``; the shipped ``prd.schema.json`` is PRODUCT.
"""

import pytest

from ops_engine.modules.mirror import (
    MirrorDestinationError,
    MirrorHandler,
    VisibilityDecision,
)

SCHEMA = (
    '{"$schema": "https://json-schema.org/draft/2020-12/schema", '
    '"title": "PRD", "type": "object", "required": ["projectName"], '
    '"properties": {"projectName": {"type": "string"}}}'
)

REDACTED_PRD = (
    '{"projectName": "Neutral placeholder wording describing the work item in '
    'general terms without naming any project", "tasks": [{"id": "T-1", '
    '"description": "Neutral placeholder wording describing the work item"}], '
    '"lane": {"creates": "Neutral placeholder wording"}}'
)

REAL_PRD = (
    '{"projectName": "Ship the unified mirror rollout", "tasks": [{"id": "T-1", '
    '"description": "The rollout script must open a pull request against main '
    'for every repo it touches, and the header must stop claiming it does when '
    'the body does not."}], "lane": {"creates": "scripts/rollout.sh"}}'
)

TEMPLATE = (
    "# PRD Template\n\n"
    "## Document Structure\n\n"
    "**Purpose:** describe the work.\n"
    "**Content:** the project name and the problem.\n"
)


def test_schema_is_product():
    d = MirrorHandler.classify_visibility(
        "skills/skillweave-blueprint/assets/prd.schema.json", content=SCHEMA
    )
    assert d.kind == "product"
    assert d.reason == "schema"


def test_redacted_fixture_is_product():
    d = MirrorHandler.classify_visibility(
        "tests/fixtures/prd-schema/forgejo-first.json", content=REDACTED_PRD
    )
    assert d.kind == "product"
    assert d.reason == "redacted"


def test_unredacted_prd_is_substrate():
    d = MirrorHandler.classify_visibility(
        "tests/fixtures/prd-schema/forgejo-first.json", content=REAL_PRD
    )
    assert d.kind == "substrate"
    assert d.reason == "planning prose"


def test_template_is_product():
    d = MirrorHandler.classify_visibility(
        "skills/skillweave-blueprint/references/prd-template.md", content=TEMPLATE
    )
    assert d.kind == "product"
    assert d.reason == "template"


def test_synthetic_sample_is_product_by_content():
    d = MirrorHandler.classify_visibility(
        "prd.json",
        content='{"projectName": "Corrected — a PRD in the format the ecosystem produces"}',
    )
    assert d.kind == "product"
    assert d.reason == "synthetic"


def test_synthetic_sample_is_product_by_name():
    d = MirrorHandler.classify_visibility(
        "tests/fixtures/prd-sample.json",
        content='{"projectName": "AI Meeting Notes Summarizer", "description": "Automatic transcription"}',
    )
    assert d.kind == "product"
    assert d.reason == "synthetic"


def test_non_planning_path_is_product_without_content_decision():
    d = MirrorHandler.classify_visibility("src/ops_engine/modules/mirror.py", content=REAL_PRD)
    assert d.kind == "product"
    assert d.reason == "not a planning class"


def test_classify_without_content_is_refused():
    with pytest.raises(MirrorDestinationError):
        MirrorHandler.classify_visibility("prd.json", content=None)


def test_substrate_files_returns_only_substrate():
    inventory = [
        "skills/skillweave-blueprint/assets/prd.schema.json",
        "tests/fixtures/prd-schema/forgejo-first.json",
        "tests/fixtures/prd-sample.json",
    ]
    contents = {
        "skills/skillweave-blueprint/assets/prd.schema.json": SCHEMA,
        "tests/fixtures/prd-schema/forgejo-first.json": REAL_PRD,
        "tests/fixtures/prd-sample.json": '{"projectName": "AI Meeting Notes Summarizer"}',
    }
    decisions = MirrorHandler.substrate_files(inventory, read=contents.get)
    assert [d.path for d in decisions] == ["tests/fixtures/prd-schema/forgejo-first.json"]
    assert all(d.kind == "substrate" for d in decisions)


def test_substrate_files_empty_tree_returns_empty():
    assert MirrorHandler.substrate_files([], read=lambda p: None) == []


def test_strategy_and_contract_paths_are_candidates():
    # A strategy file with real prose is substrate; one that is redacted is product.
    real = "Executive summary of the product strategy across all five orgs and their mirrors."
    d = MirrorHandler.classify_visibility("strategy.md", content=real)
    assert d.kind == "substrate"

    d = MirrorHandler.classify_visibility(
        "strategy.md", content="Neutral placeholder wording describing the work item"
    )
    assert d.kind == "product"
    assert d.reason == "redacted"


def test_decision_is_frozen_dataclass():
    d = VisibilityDecision(path="prd.json", kind="substrate", reason="planning prose")
    assert d.path == "prd.json"
    with pytest.raises(Exception):
        d.kind = "product"  # frozen
