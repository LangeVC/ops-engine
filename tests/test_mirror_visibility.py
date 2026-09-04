"""Visibility gate: classify by carried content, gate the ref, one rule both paths.

Consolidates the three visibility lanes' coverage after the CFG-006 rebase:

* OME-008 — ``classify_visibility`` judges what a ref CARRIES, by content (not a
  push diff). These tests exercise every code branch of the classifier with
  synthetic content (names/structure only, no real planning prose).
* OME-009 — a blocked DEFAULT branch is a release blocker and ``gate_ref`` +
  ``refuse`` say so, distinct from a refused feature branch.
* OME-010 — the event path and the full-state path apply the SAME rule
  (``gate_ref`` over the ref's full carried tree); the break-glass exact-ref
  push is OUT of the automatic gate and recorded only via ``release_override``.

The three originally separate test modules (test_mirror_visibility.py,
test_mirror_default_branch_blocker.py, test_mirror_one_rule_both_paths.py) are
merged here because the rebase's write surface for tests is this file alone.
"""

import pytest

from ops_engine.modules.mirror import (
    DefaultBranchBlockedError,
    MirrorDestinationError,
    MirrorHandler,
    Refusal,
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

# Resides in the tree but NOT in the push's diff — the exact shape of the
# OME-010 incident. A diff-only gate never sees it.
CARRIED_SUBSTRATE = "docs/proof/OPS-014-deliberate/prd.json"
DIFF_ONLY = "src/app.py"


# ─────────────────────────────────────────────────────────────────────────────
# OME-008 — classify_visibility / substrate_files
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# OME-009 — gate_ref / refuse / Refusal (default branch blocker)
# ─────────────────────────────────────────────────────────────────────────────

def _tree(substrate_path="prd.json"):
    contents = {substrate_path: REAL_PRD, "src/app.py": "print('ship')"}
    return list(contents), contents.get


def test_default_branch_refusal_is_a_release_blocker():
    paths, read = _tree()
    refusal = MirrorHandler.gate_ref("main", paths=paths, read=read)
    assert refusal is not None
    assert refusal.is_default is True
    assert refusal.is_release_blocker is True
    assert refusal.substrate == ("prd.json",)


def test_default_branch_refusal_raises_and_names_paths_and_consequence():
    paths, read = _tree()
    refusal = MirrorHandler.gate_ref("main", paths=paths, read=read)
    with pytest.raises(DefaultBranchBlockedError) as exc:
        MirrorHandler.refuse(refusal)
    msg = str(exc.value)
    assert "prd.json" in msg
    assert "BEHIND" in msg or "behind" in msg
    assert "release" in msg.lower()


def test_non_default_branch_refusal_is_not_a_release_blocker():
    paths, read = _tree()
    refusal = MirrorHandler.gate_ref("feature/x", paths=paths, read=read)
    assert refusal is not None
    assert refusal.is_default is False
    assert refusal.is_release_blocker is False
    assert refusal.substrate == ("prd.json",)


def test_non_default_branch_refusal_does_not_raise():
    paths, read = _tree()
    refusal = MirrorHandler.gate_ref("feature/x", paths=paths, read=read)
    MirrorHandler.refuse(refusal)
    assert "RELEASE BLOCKER" not in refusal.message
    assert refusal.message != MirrorHandler.gate_ref("main", paths=paths, read=read).message


def test_clean_ref_returns_none_and_refuse_is_silent():
    paths = ["src/app.py", "skills/skillweave-blueprint/assets/prd.schema.json"]
    contents = {
        "src/app.py": "print('ship')",
        "skills/skillweave-blueprint/assets/prd.schema.json": SCHEMA,
    }
    refusal = MirrorHandler.gate_ref("main", paths=paths, read=contents.get)
    assert refusal is None
    MirrorHandler.refuse(refusal)


def test_refusal_distinction_is_visible_in_messages():
    paths, read = _tree()
    default_refusal = MirrorHandler.gate_ref("main", paths=paths, read=read)
    feature_refusal = MirrorHandler.gate_ref("feature/x", paths=paths, read=read)
    assert default_refusal.message != feature_refusal.message
    assert "RELEASE BLOCKER" in default_refusal.message
    assert "BEHIND" in default_refusal.message
    assert "RELEASE BLOCKER" not in feature_refusal.message


def test_gate_ref_never_raises_on_its_own():
    paths, read = _tree()
    refusal = MirrorHandler.gate_ref("main", paths=paths, read=read)
    assert isinstance(refusal, Refusal)


def test_refusal_is_frozen_dataclass():
    r = Refusal(ref="main", is_default=True, substrate=("prd.json",))
    assert r.is_release_blocker is True
    with pytest.raises(Exception):
        r.is_default = False


# ─────────────────────────────────────────────────────────────────────────────
# OME-010 — one rule, both paths; break-glass release_override
# ─────────────────────────────────────────────────────────────────────────────

def _carried_tree_default_branch(tip_substrate=REAL_PRD):
    contents = {
        CARRIED_SUBSTRATE: tip_substrate,
        DIFF_ONLY: "print('ship')",
        "skills/skillweave-blueprint/assets/prd.schema.json": SCHEMA,
    }
    return list(contents), contents.get


def test_event_inventory_and_full_state_inventory_give_the_same_refusal():
    paths, read = _carried_tree_default_branch()
    from_event = MirrorHandler.gate_ref(
        "integration/SW-152", default_branch="main", paths=paths, read=read
    )
    from_full_state = MirrorHandler.gate_ref(
        "integration/SW-152", default_branch="main", paths=paths, read=read
    )
    assert from_event is not None
    assert from_full_state is not None
    assert from_event == from_full_state


def test_incident_shape_is_refused_though_the_diff_is_clean():
    paths, read = _carried_tree_default_branch()
    refusal = MirrorHandler.gate_ref("integration/SW-152", paths=paths, read=read)
    assert refusal is not None
    assert CARRIED_SUBSTRATE in refusal.substrate
    clean = MirrorHandler.gate_ref(
        "feature/x",
        paths=[DIFF_ONLY],
        read=lambda p: "print('ship')",
    )
    assert clean is None
    assert refusal.substrate == (CARRIED_SUBSTRATE,)


def test_default_branch_and_feature_ref_are_the_same_rule_differing_only_in_is_default():
    paths, read = _carried_tree_default_branch()
    main_r = MirrorHandler.gate_ref("main", paths=paths, read=read)
    feature_r = MirrorHandler.gate_ref("integration/JIRA-1", paths=paths, read=read)
    assert main_r.is_default is True and main_r.is_release_blocker is True
    assert feature_r.is_default is False and feature_r.is_release_blocker is False
    assert main_r.substrate == feature_r.substrate


def test_redacted_template_tree_is_clean_from_both_paths():
    contents = {
        "tests/fixtures/prd-schema/forgejo-first.json": (
            '{"projectName": "Neutral placeholder wording describing the work '
            'item in general terms without naming any project", "tasks": []}'
        ),
        "src/app.py": "print('ship')",
    }
    paths = list(contents)
    read = contents.get
    assert MirrorHandler.gate_ref("main", paths=paths, read=read) is None
    assert MirrorHandler.gate_ref("main", paths=paths, read=read) is None


def test_gate_ref_delegates_and_neither_path_classifies_again():
    paths, read = _carried_tree_default_branch()
    refusal = MirrorHandler.gate_ref("integration/SW-152", paths=paths, read=read)
    assert isinstance(refusal, Refusal)
    decisions = MirrorHandler.substrate_files(paths, read=read)
    assert isinstance(decisions, list)
    assert all(isinstance(d, VisibilityDecision) for d in decisions)


def test_refusal_produced_by_gate_has_no_override_by_default():
    paths, read = _carried_tree_default_branch()
    refusal = MirrorHandler.gate_ref("integration/SW-152", paths=paths, read=read)
    assert refusal.override_reason is None
    from dataclasses import FrozenInstanceError

    with pytest.raises(FrozenInstanceError):
        refusal.override_reason = "late"  # type: ignore[misc]


def test_release_override_requires_a_reason():
    paths, read = _carried_tree_default_branch()
    refusal = MirrorHandler.gate_ref("integration/SW-152", paths=paths, read=read)
    with pytest.raises(ValueError):
        MirrorHandler.release_override(refusal, reason="")
    with pytest.raises(ValueError):
        MirrorHandler.release_override(refusal, reason="   ")


def test_release_override_returns_a_new_refusal_and_leaves_the_original():
    paths, read = _carried_tree_default_branch()
    refusal = MirrorHandler.gate_ref("integration/SW-152", paths=paths, read=read)
    marked = MirrorHandler.release_override(
        refusal, reason="manual re-push of SW-152 after double-checking the carried PRD"
    )
    assert marked is not refusal
    assert marked.override_reason.startswith("manual re-push")
    assert refusal.override_reason is None
    assert marked.ref == refusal.ref
    assert marked.is_default == refusal.is_default
    assert marked.substrate == refusal.substrate


def test_refuse_still_raises_on_a_marked_default_branch_refusal():
    paths, read = _carried_tree_default_branch()
    main_r = MirrorHandler.gate_ref("main", paths=paths, read=read)
    marked = MirrorHandler.release_override(main_r, reason="deliberate release")
    with pytest.raises(DefaultBranchBlockedError):
        MirrorHandler.refuse(marked)


def test_refuse_is_silent_for_a_marked_feature_refusal():
    paths, read = _carried_tree_default_branch()
    feature = MirrorHandler.gate_ref("integration/SW-152", paths=paths, read=read)
    marked = MirrorHandler.release_override(feature, reason="exact-ref re-push")
    assert marked.is_default is False
    MirrorHandler.refuse(marked)


def test_default_branch_is_derived_not_hard_coded_for_the_real_default():
    paths, read = _carried_tree_default_branch()
    assert MirrorHandler.gate_ref("master", default_branch="master", paths=paths, read=read).is_default
    assert MirrorHandler.gate_ref("master", default_branch="main", paths=paths, read=read).is_default is False
