# ADP-005 — the published sdist is governed by an explicit allowlist, so a CI
# edit cannot silently change the artifact that ships.
#
# Before this lane the sdist had no `[tool.hatch.build.targets.sdist]` block at
# all, so hatchling's default whole-tree VCS selection swept the repository's
# colleagues into the tarball: .github/, .forgejo/, if a venv ever lived in the
# tree (REL-008) it would have been swept too. The wheel already declared an
# allowlist (`packages = ["src/ops_engine"]`); the sdist got none. This test
# makes the sdist's allowlist a CI gate: a stray file or directory that is not
# allowlisted fails the suite instead of shipping.
#
# The test builds from a throwaway git clone so the build never dirties the
# working tree (the build checks in this suite already follow the clone-and-
# build convention) and so the artifact is guaranteed to reflect committed
# state rather than an incidental uncommitted edit.
#
# The allowlist names the top-level components a consumer of a source
# distribution needs to rebuild the wheel: the package under src/ and the
# metadata hatchling force-includes (pyproject.toml, README, LICENSE, the VCS
# exclusion file). Any OTHER top-level entry makes the test fail.
#
# It requires a Python on PATH that can `python3 -m build` (as the suite's
# existing shell build checks already do). If the module is missing the test
# ERRORS rather than skipping, because an environment that cannot build the
# artifact cannot verify the artifact either.
#
# ADP-013 — the three content-checking tests each built a full sdist, so this
# one file cost more than the rest of the suite combined. Those three now share
# a single module-scoped build (`sdist_members`); the sweep proof below keeps
# its own second build because it must build an sdist with the allowlist
# REMOVED — a different artifact the shared build cannot serve.

import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# The only top-level components a consumer of the sdist needs. Everything a CI
# edit might otherwise leak in - .forgejo/, .github/, tests/, docs/, scripts/,
# examples/, venvs, log files - lands at a first path component that is not in
# this set and therefore fails the suite.
ALLOWED_TOP_LEVEL = {
    "src",
    "pyproject.toml",
    "README.md",
    "LICENSE",
}

# hatchling force-includes the VCS exclusion file(s) and the core metadata
# regardless of the allowlist; those are legitimate and need no separate entry.
_ALWAYS_PRESENT = {".gitignore", "PKG-INFO"}


def _top_entry(member_path: str) -> str:
    # The tarball nests every member under the versioned root directory
    # (ops_engine-3.2.0/...); the "top-level entries" of a source distribution
    # are the second component of each member path.
    return member_path.split("/", 2)[1]


def _repo_relative(member_path: str) -> str:
    # Drop the versioned root prefix (ops_engine-3.2.0/...) so the remaining
    # path is comparable to a path relative to the repository root.
    return member_path.split("/", 1)[1]


def _clone_repo(tmp: Path) -> Path:
    """Clone the repository into a throwaway directory and return its path."""
    clone_root = tmp / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(REPO_ROOT), str(clone_root)],
        check=True,
        capture_output=True,
    )
    return clone_root


def _build_sdist_members(clone_root: Path) -> set[str]:
    """Build the sdist in ``clone_root`` and return its member paths."""
    subprocess.run(
        ["python3", "-m", "build", "--sdist"],
        cwd=clone_root,
        check=True,
        capture_output=True,
        text=True,
    )
    sdist = list((clone_root / "dist").glob("*.tar.gz"))
    if len(sdist) != 1:
        raise AssertionError(f"expected exactly one sdist, found {len(sdist)}")
    with tarfile.open(sdist[0]) as tf:
        return {n for n in tf.getnames() if n and not n.endswith("/")}


@pytest.fixture(scope="module")
def sdist_members() -> set[str]:
    """One shared build serves every assertion that only inspects contents."""
    tmp = Path(tempfile.mkdtemp(prefix="adp013-"))
    try:
        clone_root = _clone_repo(tmp)
        return _build_sdist_members(clone_root)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _drop_allowlist(clone_root: Path) -> None:
    """Remove the sdist ``only-include`` allowlist from the clone's pyproject.

    With the allowlist gone, hatchling falls back to whole-tree VCS selection
    and sweeps every tracked file — the behaviour the allowlist exists to stop.
    """
    pyproject = clone_root / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    new_text = re.sub(
        r"^[ \t]*only-include[ \t]*=[ \t]*.*$",
        "",
        text,
        flags=re.MULTILINE,
    )
    assert new_text != text, "expected to remove the sdist only-include allowlist"
    pyproject.write_text(new_text, encoding="utf-8")


def test_sdist_is_an_allowlist_not_a_sweep(sdist_members: set[str]) -> None:
    members = sdist_members

    top_level = {_top_entry(m) for m in members}
    allowed = ALLOWED_TOP_LEVEL | _ALWAYS_PRESENT
    stray = sorted(top_level - allowed)
    assert stray == [], (
        "sdist strayed outside the allowlist; the next CI edit would ship "
        f"these top-level entries: {stray}"
    )

    # The sweep proof: the shared build proves nothing is stray ONCE the
    # allowlist is applied. This second build drops the allowlist and commits a
    # stray file by hand, proving the allowlist is the only thing keeping that
    # file out — without it, hatchling sweeps the stray in.
    tmp = Path(tempfile.mkdtemp(prefix="adp013-sweep-"))
    try:
        clone_root = _clone_repo(tmp)
        stray_file = clone_root / "STRAY_LEAK.txt"
        stray_file.write_text("a stray file a CI edit might leave behind\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(clone_root), "add", "STRAY_LEAK.txt", "pyproject.toml"],
            check=True,
            capture_output=True,
        )
        _drop_allowlist(clone_root)
        swept = _build_sdist_members(clone_root)
        swept_top = {_top_entry(m) for m in swept}
        assert "STRAY_LEAK.txt" in swept_top, (
            "dropping the allowlist must sweep a committed stray file into the "
            "sdist; if it does not, the allowlist guards nothing"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sdist_packages_the_committed_source(sdist_members: set[str]) -> None:
    members = sdist_members
    archived = {_repo_relative(m) for m in members if _top_entry(m) == "src"}
    # The source of truth for "what must ship" is the git index, not the
    # filesystem: a pytest run generates __pycache__/.pyc bytecode under src/
    # (gitignored, never committed) that must NOT be counted as missing when
    # the sdist correctly excludes it.
    tracked = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "src"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    committed = {p for p in tracked if p}
    missing = sorted(committed - archived)
    assert not missing, (
        "sdist is missing committed package files; a source distribution "
        f"missing these cannot rebuild the wheel: {missing}"
    )


def test_sdist_has_no_forgejo_no_venv_no_absolute_entries(
    sdist_members: set[str],
) -> None:
    members = sdist_members
    top_level = {_top_entry(m) for m in members}
    assert ".forgejo" not in top_level, (
        "sdist still carries .forgejo/; source distributions must not ship "
        "the repository's CI configuration"
    )
    assert not any(
        ".venv" in m or ".release-venv" in m or "pyvenv.cfg" in m for m in members
    ), "sdist carries a virtual environment; REL-008 forbids it"
    assert not any(m.startswith("/") for m in members), (
        "sdist carries an absolute path; reproducibility is broken"
    )


def test_sdist_is_reproducible_allowlist_input() -> None:
    """The allowlist stays internally consistent with the wheel target.

    Both the wheel and the sdist must package the SAME one package. If the two
    targets ever diverge (one packages src/ops_engine, the other a different
    tree), a consumer rebuilding from the sdist would ship a wheel that is not
    what this repository's own release job builds.
    """
    import tomllib

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel_pkgs = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    sdist_inc = pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]["only-include"]
    assert wheel_pkgs == sdist_inc, (
        f"wheel packages {wheel_pkgs} and sdist only-include {sdist_inc} disagree"
    )
