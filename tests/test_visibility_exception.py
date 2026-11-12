"""P36-001 criteria 1 to 3: the visibility gate takes an exception by path and blob.

Criterion 1: an exception entry names a path and a blob SHA; a refused commit
whose only offending content is exactly that path at exactly that blob passes
the gate.

Criterion 2: an exception never matches on the path alone. The same path at a
different blob is refused, and so is a listed blob at a different path.

Criterion 3: an unlisted refusal still blocks the push and names the commit,
the path and the blob; the exception list is read from the layover
configuration and is empty by default.

Real git, local bare repositories, forge URLs rewritten to ``file://``: no
network. The gate is exercised through both entry points (``push_event`` and
``sync_state``) because both apply one rule.
"""

import asyncio
import subprocess

import pytest

from ops_engine.config_loader import (
    Destination,
    OpsEngineConfig,
    RepoConfig,
    VisibilityException,
)
from ops_engine.modules.mirror import MirrorHandler

GLOBS = [".skillweave/**", "**/prd*.json"]
CANDIDATE = ".skillweave/prd.json"
OTHER_CANDIDATE = ".skillweave/copy.json"
ZERO = "0" * 40


def _dests():
    return [
        Destination(forge="github", repo="LangeVC/pub", role="mirror", visibility="public"),
        Destination(forge="gitlab", repo="langevc/priv", role="mirror", visibility="private"),
    ]


class _World:
    """A work repo plus local bare public, private and canonical repositories."""

    def __init__(self, tmp_path, monkeypatch):
        self.work = tmp_path / "work"
        self.work.mkdir()
        self._git("init", "-q", "-b", "main")
        self.pub = tmp_path / "pub.git"
        self.priv = tmp_path / "priv.git"
        self.canon = tmp_path / "canon.git"
        for bare in (self.pub, self.priv, self.canon):
            subprocess.run(
                ["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True
            )
        rewrites = {
            "https://github.com/LangeVC/pub.git": f"file://{self.pub}",
            "https://gitlab.com/langevc/priv.git": f"file://{self.priv}",
        }
        monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(rewrites)))
        for i, (url, local) in enumerate(rewrites.items()):
            monkeypatch.setenv(f"GIT_CONFIG_KEY_{i}", f"url.{local}.insteadOf")
            monkeypatch.setenv(f"GIT_CONFIG_VALUE_{i}", url)

    def _git(self, *args, cwd=None):
        return subprocess.run(
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

    def blob(self, path, rev="HEAD"):
        return self._git("rev-parse", f"{rev}:{path}")

    def publish_canonical(self):
        self._git("push", "-q", "-f", str(self.canon), "--all")
        self._git("push", "-q", "-f", str(self.canon), "--tags")

    def push_event(self, ref, sha, *, exceptions=None, audit=None):
        event = {"event_type": "push", "raw": {"ref": ref, "after": sha, "before": ZERO}}
        return asyncio.run(
            MirrorHandler.push_event(
                event, _dests(), repo_dir=str(self.work), audit=audit,
                candidate_globs=GLOBS,
                **({} if exceptions is None else {"visibility_exceptions": exceptions}),
            )
        )

    def sync_state(self, *, exceptions=None, audit=None):
        return asyncio.run(
            MirrorHandler.sync_state(
                f"file://{self.canon}", _dests(), audit=audit, candidate_globs=GLOBS,
                **({} if exceptions is None else {"visibility_exceptions": exceptions}),
            )
        )

    def has(self, bare, ref):
        return subprocess.run(
            ["git", "rev-parse", "--verify", "-q", ref], cwd=bare, capture_output=True
        ).returncode == 0


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = _World(tmp_path, monkeypatch)
    w.commit({"a.txt": "a\n"})
    return w


def _refused(lines):
    return [line for line in lines if line.startswith("mirror-refused")]


# ─────────────────────────────────────────────────────────────────────────────
# Criterion 1 — exactly that path at exactly that blob passes the gate
# ─────────────────────────────────────────────────────────────────────────────

def test_exception_for_the_only_offending_content_lets_the_ref_reach_public(world):
    world.branch("feature-x")
    sha = world.commit({CANDIDATE: "{}\n", "src/app.py": "ship\n"})
    blob = world.blob(CANDIDATE)
    audit = []
    lines = world.push_event(
        "refs/heads/feature-x", sha, exceptions=[(CANDIDATE, blob)], audit=audit
    )
    assert world.has(world.pub, "refs/heads/feature-x")
    assert world.has(world.priv, "refs/heads/feature-x")
    assert _refused(lines) == []
    assert audit == lines


def test_exception_also_applies_on_the_full_state_path(world):
    world.branch("excused")
    sha = world.commit({CANDIDATE: "{}\n"})
    blob = world.blob(CANDIDATE)
    world.publish_canonical()
    audit = []
    lines = world.sync_state(exceptions=[(CANDIDATE, blob)], audit=audit)
    assert world.has(world.pub, "refs/heads/excused")
    assert world.has(world.priv, "refs/heads/excused")
    assert _refused(lines) == []
    assert audit == lines


# ─────────────────────────────────────────────────────────────────────────────
# Criterion 2 — never path alone; the pair must match exactly
# ─────────────────────────────────────────────────────────────────────────────

def test_same_path_at_a_different_blob_is_refused(world):
    world.branch("feature-x")
    world.commit({CANDIDATE: '{"v": 1}\n'})
    first_blob = world.blob(CANDIDATE)
    sha = world.commit({CANDIDATE: '{"v": 2}\n'})
    second_blob = world.blob(CANDIDATE)
    assert first_blob != second_blob
    lines = world.push_event(
        "refs/heads/feature-x", sha, exceptions=[(CANDIDATE, first_blob)]
    )
    assert not world.has(world.pub, "refs/heads/feature-x")
    assert world.has(world.priv, "refs/heads/feature-x")
    refused = _refused(lines)
    assert len(refused) == 1
    assert f"{CANDIDATE}@{second_blob}" in refused[0]


def test_listed_blob_at_a_different_path_is_refused(world):
    world.branch("feature-x")
    same = '{"v": 1}\n'
    sha = world.commit({CANDIDATE: same, OTHER_CANDIDATE: same})
    blob = world.blob(CANDIDATE)
    assert world.blob(OTHER_CANDIDATE) == blob
    lines = world.push_event(
        "refs/heads/feature-x", sha, exceptions=[(CANDIDATE, blob)]
    )
    assert not world.has(world.pub, "refs/heads/feature-x")
    assert world.has(world.priv, "refs/heads/feature-x")
    refused = _refused(lines)
    assert len(refused) == 1
    assert f"{OTHER_CANDIDATE}@{blob}" in refused[0]


def test_a_path_only_exception_entry_cannot_match(world):
    world.branch("feature-x")
    sha = world.commit({CANDIDATE: "{}\n"})
    lines = world.push_event(
        "refs/heads/feature-x", sha, exceptions=[(CANDIDATE, "b" * 40)]
    )
    assert not world.has(world.pub, "refs/heads/feature-x")
    assert len(_refused(lines)) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Criterion 3 — unlisted refusal blocks and names commit, path and blob;
#               the list comes from the layover configuration, empty by default
# ─────────────────────────────────────────────────────────────────────────────

def test_unlisted_refusal_names_the_commit_path_and_blob(world):
    world.branch("feature-x")
    sha = world.commit({CANDIDATE: "{}\n"})
    blob = world.blob(CANDIDATE)
    audit = []
    lines = world.push_event("refs/heads/feature-x", sha, audit=audit)
    assert not world.has(world.pub, "refs/heads/feature-x")
    assert world.has(world.priv, "refs/heads/feature-x")
    refused = _refused(lines)
    assert len(refused) == 1
    assert f"offending={sha}:{CANDIDATE}@{blob}" in refused[0]
    assert "sha=" + sha in refused[0]
    assert audit == lines


def test_unlisted_refusal_still_blocks_on_the_full_state_path(world):
    world.branch("unexcused")
    sha = world.commit({CANDIDATE: "{}\n"})
    world.publish_canonical()
    lines = world.sync_state()
    assert not world.has(world.pub, "refs/heads/unexcused")
    assert world.has(world.priv, "refs/heads/unexcused")
    refused = _refused(lines)
    assert len(refused) == 1
    assert f"offending={sha}:{CANDIDATE}@{world.blob(CANDIDATE)}" in refused[0]


def test_the_exception_list_is_empty_by_default():
    assert RepoConfig().visibility_exceptions == []
    assert RepoConfig().visibility_exception_pairs() == ()


def test_layover_configuration_supplies_the_exception_pair():
    blob = "a" * 40
    raw = {
        "orgs": {
            "o": {
                "repositories": {
                    "r": {
                        "destinations": [
                            {"forge": "github", "repo": "o/r", "role": "mirror",
                             "visibility": "public"}
                        ],
                        "visibility_candidate_globs": GLOBS,
                        "visibility_exceptions": [{"path": CANDIDATE, "blob": blob}],
                    }
                }
            }
        }
    }
    cfg = OpsEngineConfig.load(raw).get_repo_config("o", "r")
    assert cfg.visibility_exceptions == [
        VisibilityException(path=CANDIDATE, blob=blob)
    ]
    assert cfg.visibility_exception_pairs() == ((CANDIDATE, blob),)


@pytest.mark.parametrize(
    "entry",
    [
        {"path": "", "blob": "a" * 40},
        {"path": "   ", "blob": "a" * 40},
        {"path": CANDIDATE, "blob": ""},
        {"path": CANDIDATE, "blob": "abc"},
        {"path": CANDIDATE, "blob": "A" * 40},
        {"path": CANDIDATE, "blob": "a" * 39},
    ],
)
def test_a_malformed_exception_is_refused_at_the_configuration_boundary(entry):
    with pytest.raises(ValueError):
        RepoConfig(
            visibility_exceptions=[VisibilityException(**entry)],
            visibility_candidate_globs=GLOBS,
            destinations=[
                Destination(forge="github", repo="o/r", role="mirror",
                            visibility="public")
            ],
        )
