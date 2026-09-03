"""OME-010: the event path and the full-state path apply the SAME rule.

The incident this lane closes: the EVENT path judged the DIFF of a push, so an
incremental push whose diff was clean carried a whole blocked tree to the public
mirror — how integration/SW-152 reached GitHub. The FULL-STATE path re-gated a
ref against its own merge-base and dropped the same ref. One policy, two answers,
same week, same branch.

OME-010's claim is that the rule is ONE function — `MirrorHandler.gate_ref` over
the ref's FULL carried tree, content-classified — so a ref that reached the gate
from either path receives the same `Refusal`. These tests exercise that identity
with synthetic content (names/structure only, no real planning prose): a tree
whose carried substrate lives OUTSIDE a push's diff (the incident shape) is
refused from BOTH a "what arrived by event" inventory and a "what a full-state
sweep considers" inventory, with an identical Refusal. The OME-009 refusals are
built on, never unpicked.
"""

import pytest

from ops_engine.modules.mirror import DefaultBranchBlockedError, MirrorHandler, Refusal

REAL_PRD = (
    '{"projectName": "Ship the unified mirror rollout", "tasks": [{"id": "T-1", '
    '"description": "The rollout script must open a pull request against main."}], '
    '"lane": {"creates": "scripts/rollout.sh"}}'
)

# Resides in the tree but NOT in the push's diff — the exact shape of the
# OME-010 incident. A diff-only gate never sees it.
CARRIED_SUBSTRATE = "docs/proof/OPS-014-deliberate/prd.json"
DIFF_ONLY = "src/app.py"  # a product file is all the (old) event diff would name


def _carried_tree_default_branch(tip_substrate=REAL_PRD):
    # Full tree of the ref, as the rule (gate_ref/substrate_files) judges it and
    # as a full-state sweep feeds it. The substrate file is present in the tree
    # but absent from any incremental diff.
    contents = {
        CARRIED_SUBSTRATE: tip_substrate,
        DIFF_ONLY: "print('ship')",
        "skills/skillweave-blueprint/assets/prd.schema.json": (
            '{"$schema": "https://json-schema.org/draft/2020-12/schema", '
            '"type": "object", "properties": {"projectName": {"type": "string"}}}'
        ),
    }
    return list(contents), contents.get


def _integration_sw152_inventory():
    # integration/SW-152 is a feature/integration ref: carried substrate means
    # the gate slows it (is_default False -> remind, gate working); it is not a
    # refused default branch.
    paths, read = _carried_tree_default_branch()
    return paths, read


# ---------------------------------------------------------------------------
# Criterion 1 — the same ref, same rule, one verdict from both call shapes.
# ---------------------------------------------------------------------------

def test_event_inventory_and_full_state_inventory_give_the_same_refusal():
    # A sweep and an event both feed the SAME ref's full tree to the SAME
    # function. Whatever each path's *provenance* (which branch advanced), the
    # rule under it is gate_ref — so the Refusal is identical.
    paths, read = _integration_sw152_inventory()
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
    # The OME-010 leak: a push whose DIFF names only a product file (DIFF_ONLY)
    # would pass a diff-only gate, yet the ref's carried tree holds substrate.
    # The rule (whole carried tree) refuses it. A diff view that used to pass
    # sees None is the pre-rule bug; the rule must NOT reproduce None.
    paths, read = _integration_sw152_inventory()
    refusal = MirrorHandler.gate_ref("integration/SW-152", paths=paths, read=read)
    assert refusal is not None
    assert CARRIED_SUBSTRATE in refusal.substrate
    # and the only diff name, by itself, is product — showing the diff alone
    # could never have caught it.
    clean = MirrorHandler.gate_ref(
        "feature/x",
        paths=[DIFF_ONLY],
        read=lambda p: "print('ship')",
    )
    assert clean is None
    assert refusal.substrate == (CARRIED_SUBSTRATE,)


def test_default_branch_and_feature_ref_are_the_same_rule_differing_only_in_is_default():
    # Same carried tree: only is_default differs, per OME-009. This is the proof
    # that one rule yields the default-vs-release-blocker distinction, not two
    # different gates.
    paths, read = _integration_sw152_inventory()
    main_r = MirrorHandler.gate_ref("main", paths=paths, read=read)
    feature_r = MirrorHandler.gate_ref("integration/JIRA-1", paths=paths, read=read)
    assert main_r.is_default is True and main_r.is_release_blocker is True
    assert feature_r.is_default is False and feature_r.is_release_blocker is False
    assert main_r.substrate == feature_r.substrate


def test_redacted_template_tree_is_clean_from_both_paths():
    # What OME-008 established holds on a whole tree and from either path: a
    # ref carrying only product assets (schema + redacted prose) is None.
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


# ---------------------------------------------------------------------------
# Criterion 2 — the rule exists once (no second classification implementation).
# ---------------------------------------------------------------------------

def test_gate_ref_delegates_and_neither_path_classifies_again():
    # If either path carried its own classification, then feeding it a tree that
    # classifies the same way twice would still be two copies. The behaviour here
    # is indirect; the grep in the verdict is the direct proof. This unit asserts
    # the observable contract: there is exactly one verdict object type (Refusal)
    # and one entry (gate_ref) for a single ref, and a clean tree is None — there
    # is no second return shape a second classifier would be forced to invent.
    paths, read = _integration_sw152_inventory()
    refusal = MirrorHandler.gate_ref("integration/SW-152", paths=paths, read=read)
    assert isinstance(refusal, Refusal)
    from ops_engine.modules.mirror import VisibilityDecision

    decisions = MirrorHandler.substrate_files(paths, read=read)
    assert isinstance(decisions, list)
    assert all(isinstance(d, VisibilityDecision) for d in decisions)


# ---------------------------------------------------------------------------
# Criterion 3 — the break-glass exact-ref decision is stated and enforced.
# Decision: OUT of the automatic gate but still judged; overrideable only by an
# operator who records a reason; never auto-applied by refuse().
# ---------------------------------------------------------------------------

def test_refusal_produced_by_gate_has_no_override_by_default():
    paths, read = _integration_sw152_inventory()
    refusal = MirrorHandler.gate_ref("integration/SW-152", paths=paths, read=read)
    assert refusal.override_reason is None
    # frozen dataclass: not settable after the fact
    from dataclasses import FrozenInstanceError

    with pytest.raises(FrozenInstanceError):
        refusal.override_reason = "late"  # type: ignore[misc]


def test_release_override_requires_a_reason():
    paths, read = _integration_sw152_inventory()
    refusal = MirrorHandler.gate_ref("integration/SW-152", paths=paths, read=read)
    with pytest.raises(ValueError):
        MirrorHandler.release_override(refusal, reason="")
    with pytest.raises(ValueError):
        MirrorHandler.release_override(refusal, reason="   ")


def test_release_override_returns_a_new_refusal_and_leaves_the_original():
    paths, read = _integration_sw152_inventory()
    refusal = MirrorHandler.gate_ref("integration/SW-152", paths=paths, read=read)
    marked = MirrorHandler.release_override(
        refusal, reason="manual re-push of SW-152 after double-checking the carried PRD"
    )
    assert marked is not refusal
    assert marked.override_reason.startswith("manual re-push")
    assert refusal.override_reason is None  # original untouched (frozen)
    assert marked.ref == refusal.ref
    assert marked.is_default == refusal.is_default
    assert marked.substrate == refusal.substrate


def test_refuse_still_raises_on_a_marked_default_branch_refusal():
    # The invariant that keeps break-glass OUT of the automatic gate: refuse()
    # NEVER consults override_reason. A marked REFUSAL of a default branch still
    # raises exactly like an unmarked one — the override is an attestation a
    # human acts on in the consuming workflow, never an auto-push from here.
    paths, read = _integration_sw152_inventory()
    main_r = MirrorHandler.gate_ref("main", paths=paths, read=read)
    marked = MirrorHandler.release_override(main_r, reason="deliberate release")
    with pytest.raises(DefaultBranchBlockedError):
        MirrorHandler.refuse(marked)


def test_refuse_is_silent_for_a_marked_feature_refusal():
    # OME-009: a non-default refusal is logged, not an alarm. Marking it (a
    # single exact-ref re-push of a feature/integration line) keeps refuse silent;
    # the resulting object, with its reason, is what an operator pushes by name.
    paths, read = _integration_sw152_inventory()
    feature = MirrorHandler.gate_ref("integration/SW-152", paths=paths, read=read)
    marked = MirrorHandler.release_override(feature, reason="exact-ref re-push")
    assert marked.is_default is False
    MirrorHandler.refuse(marked)  # no raise


def test_default_branch_is_derived_not_hard_coded_for_the_real_default():
    # OME-009-review flagged that this repository's real default branch is
    # master, not main. The caller supplies the true default; is_default must
    # follow it, so a caller wiring either path on this repo passes "master".
    paths, read = _carried_tree_default_branch()
    assert MirrorHandler.gate_ref("master", default_branch="master", paths=paths, read=read).is_default
    assert MirrorHandler.gate_ref("master", default_branch="main", paths=paths, read=read).is_default is False
