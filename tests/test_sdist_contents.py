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

import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

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


def _sdist_members() -> set[str]:
    """Build the sdist from a throwaway clone and return its member paths."""
    tmp = Path(tempfile.mkdtemp(prefix="adp005-"))
    try:
        clone_root = tmp / "clone"
        subprocess.run(
            ["git", "clone", "-q", str(REPO_ROOT), str(clone_root)],
            check=True,
            capture_output=True,
        )
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
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sdist_is_an_allowlist_not_a_sweep() -> None:
    members = _sdist_members()

    top_level = {_top_entry(m) for m in members}
    allowed = ALLOWED_TOP_LEVEL | _ALWAYS_PRESENT
    stray = sorted(top_level - allowed)
    assert stray == [], (
        "sdist strayed outside the allowlist; the next CI edit would ship "
        f"these top-level entries: {stray}"
    )


def test_sdist_packages_the_committed_source() -> None:
    members = _sdist_members()
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


def test_sdist_has_no_forgejo_no_venv_no_absolute_entries() -> None:
    members = _sdist_members()
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
