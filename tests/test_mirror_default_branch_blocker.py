"""OME-009: a blocked default branch is a release blocker and the gate says so.

The acceptance split is between two refusals that look identical unless the
gate records which branch they were on:

  * default branch refused  -> RELEASE BLOCKER, raised as
    ``DefaultBranchBlockedError`` whose message names the responsible paths AND
    the consequence (mirror default branch now behind; releases rejected).
  * non-default refused     -> the gate working, returned silently, message is
    the terse "refused here".

Nothing in this module retries or overrides: the only downstream action is a
raise, never a push. These tests use synthetic content (names/structure only,
no real planning prose).
"""

import pytest

from ops_engine.modules.mirror import (
    DefaultBranchBlockedError,
    MirrorHandler,
    Refusal,
)

REAL_PRD = (
    '{"projectName": "Ship the unified mirror rollout", "tasks": [{"id": "T-1", '
    '"description": "The rollout script must open a pull request against main."}], '
    '"lane": {"creates": "scripts/rollout.sh"}}'
)

SCHEMA = (
    '{"$schema": "https://json-schema.org/draft/2020-12/schema", '
    '"type": "object", "properties": {"projectName": {"type": "string"}}}'
)


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
    # names the responsible path
    assert "prd.json" in msg
    # names the consequence
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
    # refuse() is silent for a non-default refusal: no exception.
    MirrorHandler.refuse(refusal)
    # and the message differs from the blocker form
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
    MirrorHandler.refuse(refusal)  # no raise


def test_refusal_distinction_is_visible_in_messages():
    paths, read = _tree()
    default_refusal = MirrorHandler.gate_ref("main", paths=paths, read=read)
    feature_refusal = MirrorHandler.gate_ref("feature/x", paths=paths, read=read)
    assert default_refusal.message != feature_refusal.message
    assert "RELEASE BLOCKER" in default_refusal.message
    assert "BEHIND" in default_refusal.message
    assert "RELEASE BLOCKER" not in feature_refusal.message


def test_gate_ref_never_raises_on_its_own():
    # gate_ref and substrate_files are pure: they classify and return, never
    # raise the blocker (the caller decides to raise via refuse()).
    paths, read = _tree()
    refusal = MirrorHandler.gate_ref("main", paths=paths, read=read)
    assert isinstance(refusal, Refusal)


def test_refusal_is_frozen_dataclass():
    r = Refusal(ref="main", is_default=True, substrate=("prd.json",))
    assert r.is_release_blocker is True
    with pytest.raises(Exception):
        r.is_default = False
