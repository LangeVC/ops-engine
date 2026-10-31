"""OE-022 criterion 1: the event path pushes exactly the triggering ref.

On a Forgejo push webhook the layover pushes the one triggering ref to every
mirror destination with ``+ref:ref`` (force, never ``--prune`` or ``--mirror``)
and records one audit line per push. Git is replaced by a recording runner; the
refspec and flags are asserted on the exact argv.
"""

import asyncio

import pytest

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
        Destination(forge="github", repo="LangeVC/ops-engine", role="mirror", visibility="private"),
        Destination(forge="gitlab", repo="langevc/ops-engine", role="mirror", visibility="private"),
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
        assert argv[-1] == f"+{SHA}:refs/heads/feature-x"
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
    assert runner.calls[0][0][-1] == f"+{SHA}:refs/tags/v1.2.3"


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
        codes={f"+{SHA}:refs/heads/feature-x": 1}
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


class _GatedRepo:
    """A work repo plus local bare public/private mirrors (real git, no network)."""

    def __init__(self, tmp_path, monkeypatch):
        import subprocess as sp

        self.sp = sp
        self.work = tmp_path / "work"
        self.work.mkdir()
        self._git("init", "-q", "-b", "main")
        self.pub, self.priv = tmp_path / "pub.git", tmp_path / "priv.git"
        for bare in (self.pub, self.priv):
            sp.run(["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True)
        rewrites = {
            "https://github.com/LangeVC/pub.git": f"file://{self.pub}",
            "https://gitlab.com/langevc/priv.git": f"file://{self.priv}",
        }
        monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(rewrites)))
        for i, (url, local) in enumerate(rewrites.items()):
            monkeypatch.setenv(f"GIT_CONFIG_KEY_{i}", f"url.{local}.insteadOf")
            monkeypatch.setenv(f"GIT_CONFIG_VALUE_{i}", url)

    def _git(self, *args, cwd=None):
        return self.sp.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            cwd=cwd or self.work, check=True, capture_output=True, text=True,
        ).stdout.strip()

    def commit(self, files):
        for name, text in files.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self._git("add", ".")
        self._git("commit", "-q", "-m", "c")
        return self._git("rev-parse", "HEAD")

    def branch(self, name, start="HEAD"):
        self._git("checkout", "-q", "-b", name, start)

    def checkout(self, name):
        self._git("checkout", "-q", name)

    def has(self, bare, ref):
        return self.sp.run(
            ["git", "rev-parse", "--verify", "-q", ref], cwd=bare, capture_output=True
        ).returncode == 0

    def rev(self, bare, ref):
        return self._git("rev-parse", ref, cwd=bare)

    def push_event(self, ref, sha, audit, *, before=ZERO, globs=None):
        event = {"event_type": "push", "raw": {"ref": ref, "after": sha, "before": before}}
        return asyncio.run(
            MirrorHandler.push_event(
                event, _vis_dests(), repo_dir=str(self.work), audit=audit,
                candidate_globs=globs if globs is not None else CANDIDATE_GLOBS,
            )
        )


def _pushes(runner):
    return [argv for argv, _ in runner.calls if argv[1] == "push"]


def test_skillweave_path_is_refused_for_public_and_mirrored_to_private(tmp_path, monkeypatch):
    g = _GatedRepo(tmp_path, monkeypatch)
    g.commit({"a.txt": "a\n"})
    g.branch("feature-x")
    sha = g.commit({".skillweave/prds/x/prd.json": "{}\n", "src/app.py": "x\n"})
    audit = []
    lines = g.push_event("refs/heads/feature-x", sha, audit)
    assert not g.has(g.pub, "refs/heads/feature-x")
    assert g.has(g.priv, "refs/heads/feature-x")
    refused = [l for l in lines if l.startswith("mirror-refused")]
    assert len(refused) == 1
    assert "dest=github:LangeVC/pub" in refused[0]
    assert ".skillweave/prds/x/prd.json" in refused[0]
    assert audit == lines
    assert any("exit=0" in l and "dest=gitlab:langevc/priv" in l for l in lines)

def test_gate_judges_the_reachable_history_not_commits_outside_the_ref(tmp_path, monkeypatch):
    # Replaces the fork-point argv assertion (OE-035 history gate): a candidate
    # path on a commit the ref cannot reach does not refuse it; one in its own
    # reachable history does.
    g = _GatedRepo(tmp_path, monkeypatch)
    g.commit({"a.txt": "a\n"})
    g.branch("elsewhere")
    g.commit({".skillweave/other.md": "x\n"})  # on elsewhere only
    g.checkout("main")
    g.branch("clean")
    clean_sha = g.commit({"src/app.py": "x\n"})
    lines = g.push_event("refs/heads/clean", clean_sha, [])
    assert g.has(g.pub, "refs/heads/clean") and g.has(g.priv, "refs/heads/clean")
    assert not any(l.startswith("mirror-refused") for l in lines)
    g.branch("dirty", "elsewhere")
    dirty_sha = g.commit({"src/b.py": "x\n"})
    lines = g.push_event("refs/heads/dirty", dirty_sha, [])
    assert not g.has(g.pub, "refs/heads/dirty") and g.has(g.priv, "refs/heads/dirty")

def test_candidate_globs_come_from_the_passed_configuration(tmp_path, monkeypatch):
    g = _GatedRepo(tmp_path, monkeypatch)
    g.commit({"a.txt": "a\n"})
    g.branch("docs")
    docs_sha = g.commit({"docs/notes.txt": "x\n"})
    g.checkout("main")
    g.branch("skill")
    skill_sha = g.commit({".skillweave/a.md": "x\n"})
    g.push_event("refs/heads/docs", docs_sha, [], globs=["docs/**"])
    assert not g.has(g.pub, "refs/heads/docs") and g.has(g.priv, "refs/heads/docs")
    g.push_event("refs/heads/skill", skill_sha, [], globs=["docs/**"])
    # not a candidate under this configuration
    assert g.has(g.pub, "refs/heads/skill") and g.has(g.priv, "refs/heads/skill")

def test_clean_ref_is_mirrored_to_public_and_private():
    runner = GitRunner(["src/app.py"])
    _run_gated(_event(), runner, [])
    assert len(_pushes(runner)) == 2


def test_payload_default_branch_has_no_effect_and_unreadable_history_refuses():
    # The fork-point lookup (and with it the default branch) no longer decides
    # a ref; the payload value is still accepted. What stays: a history that
    # cannot be read refuses on public (fail closed) and still reaches private.
    ev = _event()
    ev["raw"]["repository"] = {"default_branch": "trunk"}
    runner = GitRunner(["src/app.py"])
    lines = _run_gated(ev, runner, [])
    assert len(_pushes(runner)) == 2 and not any(l.startswith("mirror-refused") for l in lines)

    class Unreadable(GitRunner):
        async def __call__(self, argv, cwd):
            if "log" in argv:
                self.calls.append((list(argv), cwd))
                return 128, ""
            return await super().__call__(argv, cwd)

    runner = Unreadable([])
    lines = _run_gated(_event(), runner, [])
    assert [p[2] for p in _pushes(runner)] == ["https://gitlab.com/langevc/priv.git"]
    assert any(l.startswith("mirror-refused") for l in lines)

def test_known_before_sha_never_shortens_the_judgement(tmp_path, monkeypatch):
    g = _GatedRepo(tmp_path, monkeypatch)
    c1 = g.commit({".skillweave/old.md": "x\n"})  # candidate path BEFORE `before`
    c2 = g.commit({"b.txt": "b\n"})
    c3 = g.commit({"c.txt": "c\n"})
    # The public mirror is EMPTY: it never held c2, whatever the payload claims.
    lines = g.push_event("refs/heads/main", c3, [], before=c2)
    assert any(l.startswith("mirror-refused") and ".skillweave/old.md" in l for l in lines)
    assert not g.has(g.pub, "refs/heads/main")
    assert g.rev(g.priv, "refs/heads/main") == c3
    # A candidate path inside before..sha is refused as well, and public does not advance.
    c4 = g.commit({".skillweave/new.md": "x\n"})
    lines = g.push_event("refs/heads/main", c4, [], before=c3)
    assert any(l.startswith("mirror-refused") and ".skillweave/new.md" in l for l in lines)
    assert not g.has(g.pub, "refs/heads/main")
    assert g.rev(g.priv, "refs/heads/main") == c4


# -- history gate: the payload's `before` is never trusted (P4a, P4b, P4c, P5) -----

def _sync_all(g, audit):
    canon = g.work.parent / "canon.git"
    if not canon.exists():
        g.sp.run(["git", "init", "-q", "--bare", str(canon)], check=True, capture_output=True)
    g._git("push", "-q", "-f", str(canon), "--all")
    return asyncio.run(
        MirrorHandler.sync_state(
            f"file://{canon}", _vis_dests(), audit=audit, candidate_globs=CANDIDATE_GLOBS
        )
    )


def _deliver(g, how, ref, sha, before, audit):
    if how == "event":
        return g.push_event(ref, sha, audit, before=before)
    return _sync_all(g, audit)


def _public_holds(g, sha):
    out = g.sp.run(["git", "rev-list", "--all"], cwd=g.pub, capture_output=True, text=True)
    return sha in out.stdout.split()


@pytest.mark.parametrize("how", ["event", "sync"])
def test_p4a_child_of_a_refused_ref_is_refused_whatever_before_says(tmp_path, monkeypatch, how):
    g = _GatedRepo(tmp_path, monkeypatch)
    g.commit({"a.txt": "a\n"})
    g.branch("a")
    a_sha = g.commit({".skillweave/prd.json": "{}\n"})
    _deliver(g, how, "refs/heads/a", a_sha, ZERO, [])
    g.branch("b")
    b_sha = g.commit({"b.txt": "b\n"})
    audit = []
    _deliver(g, how, "refs/heads/b", b_sha, a_sha, audit)
    assert not _public_holds(g, a_sha) and not _public_holds(g, b_sha)
    assert not g.has(g.pub, "refs/heads/b") and g.has(g.priv, "refs/heads/b")
    assert any(l.startswith("mirror-refused") and "ref=refs/heads/b" in l for l in audit)


@pytest.mark.parametrize("how", ["event", "sync"])
def test_p4b_default_branch_after_a_refused_push_stays_refused(tmp_path, monkeypatch, how):
    g = _GatedRepo(tmp_path, monkeypatch)
    c1 = g.commit({"a.txt": "a\n"})
    c2 = g.commit({".skillweave/prd.json": "{}\n"})
    _deliver(g, how, "refs/heads/main", c2, c1, [])
    c3 = g.commit({"c.txt": "c\n"})
    audit = []
    _deliver(g, how, "refs/heads/main", c3, c2, audit)
    assert not _public_holds(g, c2) and not _public_holds(g, c3)
    assert any(l.startswith("mirror-refused") for l in audit)


@pytest.mark.parametrize("how", ["event", "sync"])
def test_p4c_before_the_destination_never_held_does_not_hide_history(tmp_path, monkeypatch, how):
    g = _GatedRepo(tmp_path, monkeypatch)
    c1 = g.commit({".skillweave/prd.json": "{}\n"})
    c2 = g.commit({"b.txt": "b\n"})
    audit = []
    _deliver(g, how, "refs/heads/main", c2, c1, audit)
    assert not _public_holds(g, c1) and not _public_holds(g, c2)
    assert any(l.startswith("mirror-refused") for l in audit)


@pytest.mark.parametrize("how", ["event", "sync"])
def test_p5_non_ascii_candidate_name_does_not_dodge_the_glob(tmp_path, monkeypatch, how):
    g = _GatedRepo(tmp_path, monkeypatch)
    g.commit({"a.txt": "a\n"})
    sha = g.commit({".skillweave/pr\u00e4d.json": "{}\n"})
    audit = []
    _deliver(g, how, "refs/heads/main", sha, ZERO, audit)
    assert not _public_holds(g, sha)
    assert any(l.startswith("mirror-refused") and "pr\u00e4d.json" in l for l in audit)

def test_config_layer_declares_candidate_globs():
    from ops_engine.config_loader import RepoConfig

    assert ".skillweave/**" in RepoConfig().visibility_candidate_globs
    assert RepoConfig(visibility_candidate_globs=["x/**"]).visibility_candidate_globs == ["x/**"]


# ── Real git, real runner (runner=None), local bare repos, no network ────────

import os
import subprocess

import pytest


def _git(cwd, *args, env=None):
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True,
        env={**os.environ, **(env or {})},
    )


def _rev(cwd, name):
    return subprocess.run(
        ["git", "rev-parse", name], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def real(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    _git(work, "init", "-q", "-b", "main")
    (work / "a.txt").write_text("a\n")
    _git(work, "add", ".")
    _git(work, *ident, "commit", "-q", "-m", "base")
    pub, priv = tmp_path / "pub.git", tmp_path / "priv.git"
    for bare in (pub, priv):
        _git(tmp_path, "init", "-q", "--bare", str(bare))
    # Route the fixed forge URLs to the local bare repos (no network).
    rewrites = {
        "https://github.com/LangeVC/pub.git": f"file://{pub}",
        "https://gitlab.com/langevc/priv.git": f"file://{priv}",
    }
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(rewrites)))
    for i, (url, local) in enumerate(rewrites.items()):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{i}", f"url.{local}.insteadOf")
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{i}", url)
    return work, pub, priv, ident


def _real_push(work, ref, before, audit):
    event = {
        "event_type": "push",
        "repo": "x/y",
        "raw": {"ref": ref, "after": _rev(work, ref), "before": before,
                "repository": {"default_branch": "main"}},
    }
    return asyncio.run(
        MirrorHandler.push_event(
            event, _vis_dests(), repo_dir=str(work), audit=audit,
            candidate_globs=CANDIDATE_GLOBS,
        )
    )


def _has_ref(bare, ref):
    return subprocess.run(
        ["git", "rev-parse", "--verify", "-q", ref], cwd=bare, capture_output=True
    ).returncode == 0


def test_real_git_public_refuses_skillweave_branch_private_receives_it(real):
    work, pub, priv, ident = real
    _git(work, "checkout", "-q", "-b", "feature")
    (work / ".skillweave").mkdir()
    (work / ".skillweave" / "x.md").write_text("plan\n")
    _git(work, "add", ".")
    _git(work, *ident, "commit", "-q", "-m", "plan")
    audit = []
    lines = _real_push(work, "refs/heads/feature", "0" * 40, audit)
    assert not _has_ref(pub, "refs/heads/feature")
    assert _has_ref(priv, "refs/heads/feature")
    assert any(l.startswith("mirror-refused") and ".skillweave/x.md" in l for l in lines)
    assert any("dest=gitlab:langevc/priv" in l and "exit=0" in l for l in lines)
    assert audit == lines


def test_real_git_clean_branch_reaches_public_and_private(real):
    work, pub, priv, ident = real
    _git(work, "checkout", "-q", "-b", "feature")
    (work / "b.txt").write_text("b\n")
    _git(work, "add", ".")
    _git(work, *ident, "commit", "-q", "-m", "b")
    _real_push(work, "refs/heads/feature", "0" * 40, [])
    assert _has_ref(pub, "refs/heads/feature") and _has_ref(priv, "refs/heads/feature")


def test_real_git_first_push_of_default_branch_judges_the_whole_tree(real):
    work, pub, priv, ident = real
    (work / ".skillweave").mkdir()
    (work / ".skillweave" / "x.md").write_text("plan\n")
    _git(work, "add", ".")
    _git(work, *ident, "commit", "-q", "-m", "plan on main")
    lines = _real_push(work, "refs/heads/main", "0" * 40, [])
    assert not _has_ref(pub, "refs/heads/main")
    assert _has_ref(priv, "refs/heads/main")
    assert any(l.startswith("mirror-refused") and ".skillweave/x.md" in l for l in lines)


# -- judged == pushed: the object that is sent is the object that was checked ------

def test_m1_ref_moved_after_the_event_does_not_ship_the_newer_commit(tmp_path, monkeypatch):
    g = _GatedRepo(tmp_path, monkeypatch)
    g.commit({"a.txt": "a\n"})
    g.branch("feat")
    a = g.commit({"b.txt": "b\n"})
    b = g.commit({".skillweave/prd.json": "{}\n"})  # local ref moved on to B
    lines = g.push_event("refs/heads/feat", a, [], before=ZERO)
    assert g.rev(g.pub, "refs/heads/feat") == a  # exactly what was judged
    assert not _public_holds(g, b)
    assert not any(l.startswith("mirror-refused") for l in lines)


def test_m2_after_of_another_branch_cannot_carry_the_dirty_ref(tmp_path, monkeypatch):
    g = _GatedRepo(tmp_path, monkeypatch)
    clean = g.commit({"a.txt": "a\n"})
    g.branch("dirty")
    dirty = g.commit({".skillweave/prd.json": "{}\n"})
    g.push_event("refs/heads/dirty", clean, [], before=ZERO)
    assert not _public_holds(g, dirty)
    assert g.rev(g.pub, "refs/heads/dirty") == clean


@pytest.mark.parametrize("after", ["--upload-pack=touch /tmp/x", "-f", "zz" * 20, "A" * 40, "abc"])
def test_invalid_after_is_refused_audited_and_nothing_is_pushed(after):
    runner, audit = Runner(), []
    lines = _run_gated(_event(after=after), runner, audit)
    assert runner.calls == []
    assert len(lines) == 1 and lines == audit
    assert lines[0].startswith("mirror-refused") and "reason=invalid-sha" in lines[0]


# -- only an object that peels to a commit is judged (tree, blob, tag-of-tree) ------

def _holds_object(g, sha):
    return g.sp.run(
        ["git", "cat-file", "-e", sha], cwd=g.pub, capture_output=True
    ).returncode == 0


def _dirty_objects(g):
    g.commit({"a.txt": "a\n"})
    g.commit({".skillweave/prd.json": "{}\n"})
    tree = g._git("rev-parse", "HEAD^{tree}")
    blob = g._git("rev-parse", "HEAD:.skillweave/prd.json")
    g._git("tag", "-a", "-m", "t", "tag-of-tree", tree)
    return {"tree": tree, "blob": blob, "tag-of-tree": g._git("rev-parse", "tag-of-tree")}


@pytest.mark.parametrize("kind", ["tree", "blob", "tag-of-tree"])
def test_non_commit_after_is_refused_and_never_reaches_public(tmp_path, monkeypatch, kind):
    g = _GatedRepo(tmp_path, monkeypatch)
    sha = _dirty_objects(g)[kind]
    g._git("tag", "t", sha)
    audit = []
    lines = g.push_event("refs/tags/t", sha, audit)
    assert not _holds_object(g, sha)
    assert not g.has(g.pub, "refs/tags/t")
    assert any(l.startswith("mirror-refused") and "<not a commit>" in l for l in lines)
    assert audit == lines


def test_lightweight_tag_on_a_dirty_tree_is_refused_by_sync_state(tmp_path, monkeypatch):
    g = _GatedRepo(tmp_path, monkeypatch)
    objs = _dirty_objects(g)
    g._git("tag", "lw", objs["tree"])
    audit = []
    _sync_all_with_tags(g, audit)
    assert not g.has(g.pub, "refs/tags/lw") and not _holds_object(g, objs["tree"])
    assert any("ref=refs/tags/lw" in l and "<not a commit>" in l for l in audit)


def _sync_all_with_tags(g, audit):
    canon = g.work.parent / "canon.git"
    g.sp.run(["git", "init", "-q", "--bare", str(canon)], check=True, capture_output=True)
    g._git("push", "-q", "-f", str(canon), "--all")
    g._git("push", "-q", "-f", str(canon), "--tags")
    return asyncio.run(
        MirrorHandler.sync_state(
            f"file://{canon}", _vis_dests(), audit=audit, candidate_globs=CANDIDATE_GLOBS
        )
    )


def test_clean_annotated_tag_still_reaches_public(tmp_path, monkeypatch):
    g = _GatedRepo(tmp_path, monkeypatch)
    g.commit({"a.txt": "a\n"})
    g._git("tag", "-a", "-m", "v1", "v1")
    tag_obj = g._git("rev-parse", "v1")
    lines = g.push_event("refs/tags/v1", tag_obj, [])
    assert not any(l.startswith("mirror-refused") for l in lines)
    assert g.rev(g.pub, "refs/tags/v1") == tag_obj and g.has(g.priv, "refs/tags/v1")


@pytest.mark.parametrize("after", [12345, ["a" * 40], {"id": "a" * 40}, 1.5, True])
def test_non_string_after_is_refused_audited_once_and_nothing_is_pushed(after):
    runner, audit = Runner(), []
    event = {"event_type": "push", "raw": {"ref": "refs/heads/x", "after": after}}
    lines = asyncio.run(
        MirrorHandler.push_event(event, _vis_dests(), repo_dir="/x", runner=runner, audit=audit)
    )
    assert runner.calls == []
    assert len(lines) == 1 and lines == audit and "reason=invalid-sha" in lines[0]
