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


# ── OE-022 criteria 2 and 3: visibility gate on public destinations ──────────

FORK = "b" * 40
CANDIDATE_GLOBS = [".skillweave/**", "**/prd*.json"]


class GitRunner(Runner):
    """Recording runner that answers merge-base and diff like git would."""

    def __init__(self, changed, merge_base=FORK, base_code=0):
        super().__init__()
        self.changed = changed
        self.merge_base = merge_base
        self.base_code = base_code

    async def __call__(self, argv, cwd):
        if argv[1] == "merge-base":
            self.calls.append((list(argv), cwd))
            return self.base_code, self.merge_base + "\n" if self.base_code == 0 else ""
        if argv[1] == "diff":
            self.calls.append((list(argv), cwd))
            return 0, "\n".join(self.changed) + "\n"
        return await super().__call__(argv, cwd)


def _vis_dests():
    return [
        Destination(forge="github", repo="LangeVC/pub", role="mirror", visibility="public"),
        Destination(forge="gitlab", repo="langevc/priv", role="mirror", visibility="private"),
    ]


def _run_gated(event, runner, audit, globs=CANDIDATE_GLOBS):
    return asyncio.run(
        MirrorHandler.push_event(
            event,
            _vis_dests(),
            repo_dir="/work/repo",
            runner=runner,
            audit=audit,
            candidate_globs=globs,
        )
    )


def _pushes(runner):
    return [argv for argv, _ in runner.calls if argv[1] == "push"]


def test_skillweave_path_is_refused_for_public_and_mirrored_to_private():
    runner, audit = GitRunner([".skillweave/prds/x/prd.json", "src/app.py"]), []
    lines = _run_gated(_event(), runner, audit)
    pushes = _pushes(runner)
    assert [p[2] for p in pushes] == ["https://gitlab.com/langevc/priv.git"]
    refused = [l for l in lines if l.startswith("mirror-refused")]
    assert len(refused) == 1
    assert "dest=github:LangeVC/pub" in refused[0]
    assert ".skillweave/prds/x/prd.json" in refused[0]
    assert audit == lines
    assert any("exit=0" in l and "dest=gitlab:langevc/priv" in l for l in lines)


def test_gate_diffs_from_the_fork_point_with_the_default_branch():
    runner = GitRunner(["src/app.py"])
    _run_gated(_event(), runner, [])
    assert runner.calls[0][0] == ["git", "merge-base", "refs/heads/main", SHA]
    assert runner.calls[1][0] == ["git", "diff", "--name-only", FORK, SHA]


def test_candidate_globs_come_from_the_passed_configuration():
    runner = GitRunner(["docs/notes.txt"])
    _run_gated(_event(), runner, [], globs=["docs/**"])
    assert [p[2] for p in _pushes(runner)] == ["https://gitlab.com/langevc/priv.git"]
    runner = GitRunner([".skillweave/a.md"])
    _run_gated(_event(), runner, [], globs=["docs/**"])
    assert len(_pushes(runner)) == 2  # not a candidate under this configuration


def test_clean_ref_is_mirrored_to_public_and_private():
    runner = GitRunner(["src/app.py"])
    _run_gated(_event(), runner, [])
    assert len(_pushes(runner)) == 2


def test_default_branch_uses_payload_default_and_unresolved_fork_point_refuses():
    ev = _event()
    ev["raw"]["repository"] = {"default_branch": "trunk"}
    runner = GitRunner(["src/app.py"])
    _run_gated(ev, runner, [])
    assert runner.calls[0][0][2] == "refs/heads/trunk"
    runner = GitRunner(["src/app.py"], base_code=128)
    lines = _run_gated(_event(), runner, [])
    assert [p[2] for p in _pushes(runner)] == ["https://gitlab.com/langevc/priv.git"]
    assert any(l.startswith("mirror-refused") for l in lines)


def test_default_branch_push_is_diffed_from_the_before_sha():
    ev = _event(ref="refs/heads/main")
    ev["raw"]["before"] = "c" * 40
    runner = GitRunner([".skillweave/x"])
    _run_gated(ev, runner, [])
    assert runner.calls[0][0] == ["git", "diff", "--name-only", "c" * 40, SHA]
    assert [p[2] for p in _pushes(runner)] == ["https://gitlab.com/langevc/priv.git"]


def test_config_layer_declares_candidate_globs():
    from ops_engine.config_loader import RepoConfig

    assert ".skillweave/**" in RepoConfig().visibility_candidate_globs
    assert RepoConfig(visibility_candidate_globs=["x/**"]).visibility_candidate_globs == ["x/**"]
