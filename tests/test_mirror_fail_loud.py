"""OE-036 criteria 1 and 2: the sweep protects the real default branch, and
stderr is never parsed as ls-remote output."""

import asyncio
import os
import stat

from ops_engine.config_loader import Destination
from ops_engine.modules.mirror import MirrorHandler

CANON = "https://forgejo.example/langevc/ops-engine.git"
GH = "https://github.com/LangeVC/ops-engine.git"


def _dest():
    return Destination(forge="github", repo="LangeVC/ops-engine", role="mirror", visibility="private")


class _Runner:
    def __init__(self, head, canonical_refs, mirror_refs):
        self.calls = []
        self.head = head
        self.canonical_refs = canonical_refs
        self.mirror_refs = mirror_refs

    async def __call__(self, argv, cwd):
        self.calls.append(list(argv))
        if argv[1] == "symbolic-ref":
            return 0, self.head + "\n"
        if argv[1] == "for-each-ref":
            return 0, "\n".join(self.canonical_refs) + "\n"
        if argv[1] == "ls-remote":
            return 0, "\n".join(f"{'a' * 40}\t{r}" for r in self.mirror_refs) + "\n"
        return 0, ""

    def deletes(self):
        return [a[3] for a in self.calls if a[1] == "push"]


def test_master_default_branch_of_the_canonical_clone_is_kept():
    runner = _Runner(
        head="refs/heads/master",
        canonical_refs=["refs/heads/other"],
        mirror_refs=["refs/heads/master", "refs/heads/stale"],
    )
    lines = asyncio.run(MirrorHandler.sweep_deleted_refs(CANON, [_dest()], runner=runner))
    assert runner.deletes() == [":refs/heads/stale"]
    assert any(l.startswith("mirror-ref-kept ref=refs/heads/master") for l in lines)


def test_unreadable_default_branch_deletes_nothing():
    class Unreadable(_Runner):
        async def __call__(self, argv, cwd):
            if argv[1] == "symbolic-ref":
                self.calls.append(list(argv))
                return 128, ""
            return await super().__call__(argv, cwd)

    runner = Unreadable("", ["refs/heads/other"], ["refs/heads/master", "refs/heads/stale"])
    lines = asyncio.run(MirrorHandler.sweep_deleted_refs(CANON, [_dest()], runner=runner))
    assert runner.deletes() == []
    assert any("mirror-sweep-aborted reason=canonical-head-unreadable" in l for l in lines)


def test_two_word_stderr_line_is_not_read_as_a_ref(tmp_path, monkeypatch):
    log = tmp_path / "pushes.log"
    fake = tmp_path / "bin" / "git"
    fake.parent.mkdir()
    fake.write_text(
        "#!/bin/bash\n"
        'case "$1" in\n'
        '  clone) mkdir -p "$4" ;;\n'
        '  symbolic-ref) echo refs/heads/main ;;\n'
        '  for-each-ref) echo refs/heads/main ;;\n'
        '  ls-remote) echo "warning: redirecting" >&2; echo "' + "a" * 40 + '\trefs/heads/main" ;;\n'
        f'  push) echo "$@" >> "{log}" ;;\n'
        "esac\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    asyncio.run(MirrorHandler.sweep_deleted_refs(CANON, [_dest()], workdir=str(tmp_path)))
    assert not log.exists(), log.read_text()
