"""OE-022 criterion 1: the event path pushes exactly the triggering ref.

On a Forgejo push webhook the layover pushes the one triggering ref to every
mirror destination with ``+ref:ref`` (force, never ``--prune`` or ``--mirror``)
and records one audit line per push. Git is replaced by a recording runner; the
refspec and flags are asserted on the exact argv.
"""

import asyncio

from ops_engine.config_loader import Destination
from ops_engine.modules.mirror import MirrorHandler

SHA = "a" * 40
ZERO = "0" * 40


class Runner:
    def __init__(self, codes=None):
        self.calls = []
        self.codes = codes or {}

    async def __call__(self, argv, cwd):
        self.calls.append((list(argv), cwd))
        return self.codes.get(argv[-1], 0), ""


def _event(ref="refs/heads/feature-x", after=SHA):
    return {
        "event_type": "push",
        "repo": "langevc/ops-engine",
        "raw": {"ref": ref, "after": after},
    }


def _dests():
    return [
        Destination(forge="github", repo="LangeVC/ops-engine", role="mirror"),
        Destination(forge="gitlab", repo="langevc/ops-engine", role="mirror"),
        Destination(forge="github", repo="LangeVC/ops-engine-rel", role="release"),
    ]


def _run(event, dests, runner, audit):
    return asyncio.run(
        MirrorHandler.push_event(
            event, dests, repo_dir="/work/repo", runner=runner, audit=audit
        )
    )


def test_pushes_exactly_the_triggering_ref_to_every_mirror_destination():
    runner, audit = Runner(), []
    _run(_event(), _dests(), runner, audit)
    assert len(runner.calls) == 2  # two mirror destinations, release skipped
    for argv, cwd in runner.calls:
        assert cwd == "/work/repo"
        assert argv[:2] == ["git", "push"]
        assert argv[-1] == "+refs/heads/feature-x:refs/heads/feature-x"
    assert runner.calls[0][0][2] == "https://github.com/LangeVC/ops-engine.git"
    assert runner.calls[1][0][2] == "https://gitlab.com/langevc/ops-engine.git"


def test_never_prunes_or_mirrors_and_pushes_one_refspec():
    runner = Runner()
    _run(_event(), _dests(), runner, [])
    for argv, _ in runner.calls:
        assert not any(a in ("--prune", "--mirror", "--all", "--tags") for a in argv)
        assert [a for a in argv if ":" in a and a.startswith("+")] == [argv[-1]]
        assert len(argv) == 4


def test_tag_ref_is_pushed_as_itself():
    runner = Runner()
    _run(_event(ref="refs/tags/v1.2.3"), _dests(), runner, [])
    assert runner.calls[0][0][-1] == "+refs/tags/v1.2.3:refs/tags/v1.2.3"


def test_one_audit_line_per_push():
    runner, audit = Runner(codes={}), []
    _run(_event(), _dests(), runner, audit)
    assert len(audit) == 2
    assert "ref=refs/heads/feature-x" in audit[0]
    assert "dest=github:LangeVC/ops-engine" in audit[0]
    assert f"sha={SHA}" in audit[0]
    assert "exit=0" in audit[0]
    assert "dest=gitlab:langevc/ops-engine" in audit[1]


def test_failed_push_is_audited_and_does_not_stop_the_other_destination():
    runner = Runner(
        codes={"+refs/heads/feature-x:refs/heads/feature-x": 1}
    )
    audit = []
    _run(_event(), _dests(), runner, audit)
    assert len(runner.calls) == 2
    assert all("exit=1" in line for line in audit)


def test_non_push_event_pushes_nothing():
    runner, audit = Runner(), []
    ev = _event()
    ev["event_type"] = "issues"
    _run(ev, _dests(), runner, audit)
    assert runner.calls == [] and audit == []


def test_branch_deletion_pushes_nothing():
    runner, audit = Runner(), []
    _run(_event(after=ZERO), _dests(), runner, audit)
    assert runner.calls == [] and audit == []
