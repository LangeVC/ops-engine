#!/usr/bin/env bash
# LNF-100-2 / LVC-231 — the public OSS template ships no build output.
#
# ops-engine master carried 41 tracked __pycache__/*.pyc files and .gitignore
# had no entry for pycache, pyc or egg-info. A checked-in .pyc is imported in
# preference to the source when its hash or timestamp matches; after the
# typed-config change in v3.0.0 a stale config_loader.cpython-314.pyc is a
# defect nobody can reproduce from the tree.
#
# Acceptance criteria:
#   1. all tracked __pycache__/*.pyc files are removed
#   2. .gitignore covers __pycache__/, *.py[cod] and *.egg-info/
#   3. on a fresh clone, `git ls-files | grep -c pyc` is 0, and a test run
#      leaves the tree clean
#   4. an sdist and a wheel built from the cleaned tree contain no bytecode
#
# Precondition (must be red before the fix): `git ls-files | grep -c pyc`
# returns a non-zero count.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

failures=0
fail() { echo "FAIL: $1"; failures=$((failures + 1)); }

# --- 1. no tracked bytecode in the tree --------------------------------------
tracked_pyc=$(git ls-files | grep -c '\.pyc$' || true)
tracked_pyo=$(git ls-files | grep -c '\.pyo$' || true)
[[ "$tracked_pyc" -eq 0 ]] || fail "1: $tracked_pyc tracked .pyc file(s) remain"
[[ "$tracked_pyo" -eq 0 ]] || fail "1: $tracked_pyo tracked .pyo file(s) remain"

# --- 2. .gitignore covers the build-artifact patterns -------------------------
grep -qF '__pycache__/' .gitignore || fail "2: .gitignore does not cover '__pycache__/'"
grep -qF '*.py[cod]' .gitignore  || fail "2: .gitignore does not cover '*.py[cod]'"
grep -qF '*.egg-info/' .gitignore || fail "2: .gitignore does not cover '*.egg-info/'"

# --- 3. fresh clone: no tracked pyc, test run leaves the tree clean -----------
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
git clone -q "$REPO_ROOT" "$TMP/clone"
cd "$TMP/clone"

clone_pyc=$(git ls-files | grep -c '\.pyc$' || true)
[[ "$clone_pyc" -eq 0 ]] || fail "3: fresh clone has $clone_pyc tracked .pyc file(s)"

if ! PYTHONPATH=src python3 -m pytest tests/ -q >/dev/null 2>&1; then
    fail "3: pytest run in the fresh clone failed"
fi

dirty=$(git status --porcelain | wc -l | tr -d ' ')
[[ "$dirty" -eq 0 ]] || fail "3: test run left $dirty untracked/modified file(s) in the fresh clone"

# --- 4. sdist and wheel from the cleaned tree contain no bytecode -------------
if ! python3 -m build --sdist --wheel >/dev/null 2>&1; then
    fail "4: python3 -m build (sdist+wheel) failed; is the 'build' package installed?"
fi

set +e
python3 - <<'PY' > "$TMP/artifact_check.out" 2>&1
import glob, tarfile, zipfile, sys

bad = []
for w in glob.glob("dist/*.whl"):
    names = zipfile.ZipFile(w).namelist()
    bad += [(w, n) for n in names if n.endswith(".pyc") or "__pycache__" in n]
for s in glob.glob("dist/*.tar.gz"):
    names = tarfile.open(s).getnames()
    bad += [(s, n) for n in names if n.endswith(".pyc") or "__pycache__" in n]
if bad:
    for a, n in bad:
        print(f"  bytecode in {a}: {n}")
    sys.exit(1)
if not glob.glob("dist/*.whl") and not glob.glob("dist/*.tar.gz"):
    sys.exit(1)
print("artifacts clean: no bytecode")
PY
artifact_exit=$?
set -e
if [[ "$artifact_exit" -ne 0 ]]; then
    cat "$TMP/artifact_check.out"
    fail "4: a built artifact contains stray bytecode"
fi

cd "$REPO_ROOT"

if (( failures > 0 )); then
    echo "test_no_build_artifacts: FAIL ($failures)"
    exit 1
fi
echo "test_no_build_artifacts: PASS"
