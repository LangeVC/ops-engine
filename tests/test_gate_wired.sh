#!/usr/bin/env bash
# FFR-600-9 — the release gate is wired on main and on dev through one code
# path, not a copy.
#
# Runs under bash -eo pipefail, the same shell and flags as the runner.
#
# Checks:
#   1. The workflow .forgejo/workflows/release-gate.yml exists, triggers on v*
#      tags, and invokes scripts/version-sync.py check-tag exactly once.
#   2. REL-023: the version gate is deliberately present in BOTH workflows.
#      release-gate.yml is the standalone gate a tag push starts; the gate job
#      inside forgejo-release.yml is the one the release job needs, and a
#      release-gate verdict cannot stop a release because the two push-triggered
#      workflows do not wait on each other. Each file invokes check-tag exactly
#      once, so neither carries a copied code path inside itself. What must NOT
#      exist anywhere is a second release-object WRITER: that property is
#      checked here through scripts/release-writer-check.py over both forges.
#   3. Red proof: a tag whose declared version lags turns the gate red before
#      any release step is reached.
#   4. Dev rc proof: a prerelease tag is refused unless --allow-prereleases is
#      derived, and the same gate turns green when it is.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

WORKFLOW=".forgejo/workflows/release-gate.yml"
SCRIPT="scripts/version-sync.py"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass=0
fail=0
ok()  { printf 'PASS: %s\n' "$1"; pass=$((pass+1)); }
bad() { printf 'FAIL: %s\n' "$1"; fail=$((fail+1)); }

run() {
    if "$@" >"$TMP/out" 2>"$TMP/err"; then
        return 0
    else
        return 1
    fi
}

gate() {
    local repo="$1" tag="$2"
    shift 2
    run python3 "$SCRIPT" check-tag "$tag" --repo "$repo" "$@"
}

# --- 1. wiring: the workflow exists and invokes the shared gate -------------
[[ -f "$WORKFLOW" ]] || bad "workflow $WORKFLOW does not exist"
grep -q 'tags:' "$WORKFLOW" || bad "workflow does not trigger on tags"
grep -qE '"v\*"' "$WORKFLOW" || bad "workflow does not trigger on v* tags"
grep -q 'version-sync.py' "$WORKFLOW" || bad "workflow does not invoke scripts/version-sync.py"
if grep -q -- '--allow-prereleases' "$WORKFLOW"; then
    ok "workflow derives the --allow-prereleases flag for the dev rc run"
else
    bad "workflow never derives the --allow-prereleases flag for the dev rc run"
fi

# --- 2. REL-023: the gate in both workflows, the writer only in the engine ---
# The version gate is intentionally present in both files: release-gate.yml for
# a tag push, and the gate job inside forgejo-release.yml whose release job
# declares `needs: gate`. Both invoke the same check-tag script once each; the
# duplication that FFR-600-9 forbids is a copied gate WITHIN one file, and a
# second release-object writer ANYWHERE. The first is checked per file below;
# the second is delegated to the committed census script.
RELEASE_WORKFLOW=".forgejo/workflows/forgejo-release.yml"

for wf in "$WORKFLOW" "$RELEASE_WORKFLOW"; do
    if [[ -f "$wf" ]]; then
        ok "version gate workflow exists: $wf"
    else
        bad "version gate workflow missing: $wf"
        continue
    fi
    n_invocations=$(grep -c 'check-tag' "$wf")
    if [[ "$n_invocations" -eq 1 ]]; then
        ok "$wf invokes check-tag once (one code path per file)"
    else
        bad "$wf invokes check-tag $n_invocations times; expected one code path, not a copy"
    fi
done

if grep -A2 '^  release:' "$RELEASE_WORKFLOW" | grep -q 'needs: gate'; then
    ok "the release job needs the in-file gate (REL-023)"
else
    bad "the release job does not declare needs: gate (a red gate could not stop it)"
fi

if python3 scripts/release-writer-check.py >"$TMP/writer" 2>&1; then
    ok "no release-object writer outside the engine path (both forges)"
else
    cat "$TMP/writer" >&2
    bad "a workflow writes a release object outside the engine path"
fi

# --- 3. red proof: mismatched version turns the gate red before release -----
_make_repo() {
    local dir="$1" version="$2"
    mkdir -p "$dir"
    cat > "$dir/.version.yaml" <<'EOF'
schema: 1
source_of_truth: version.txt
locations:
  - path: version.txt
    pattern: '^(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$'
    required: true
EOF
    printf '%s\n' "$version" > "$dir/version.txt"
}

push_gate_then_release() {
    local repo="$1" tag="$2"
    shift 2
    if gate "$repo" "$tag" "$@"; then
        touch "$repo/release-reached.marker"
        return 0
    fi
    return 1
}

redproof="$TMP/redproof"
_make_repo "$redproof" "3.0.0"

if push_gate_then_release "$redproof" "v3.0.0"; then
    ok "red proof — matching version -> gate green"
    [[ -e "$redproof/release-reached.marker" ]] \
        && ok "red proof — green gate reaches release" \
        || bad "red proof — green gate did not reach release"
else
    bad "red proof — matching version -> expected gate green"
fi

printf '2.9.0\n' > "$redproof/version.txt"
rm -f "$redproof/release-reached.marker"
if push_gate_then_release "$redproof" "v3.0.0"; then
    bad "red proof — mismatched version -> expected gate red"
else
    ok "red proof — mismatched version -> gate red"
fi
[[ -e "$redproof/release-reached.marker" ]] \
    && bad "red proof — release step ran despite the red gate" \
    || ok "red proof — red gate fired before any release step"

# --- 4. dev rc proof: same gate, one derived flag ----------------------------
rcproof="$TMP/rcproof"
_make_repo "$rcproof" "3.1.0-rc.1"

if gate "$rcproof" "v3.1.0-rc.1"; then
    bad "rc gate — prerelease tag without --allow-prereleases -> expected red"
else
    ok "rc gate — prerelease tag without --allow-prereleases -> red"
fi

if gate "$rcproof" "v3.1.0-rc.1" --allow-prereleases; then
    ok "rc gate — prerelease tag with --allow-prereleases -> green (same gate)"
else
    bad "rc gate — prerelease tag with --allow-prereleases -> expected green"
fi

# --- 5. real case: this repo's own version home and its real tag --------------
# The synthetic proofs above fabricate a version.txt. Here the gate runs against
# the repository's actual canonical version location (pyproject.toml). The tag
# under test is derived from the version the live pyproject.toml declares — not
# from a frozen tag — so the same gate is exercised on every future bump with no
# edit. A deliberately mismatched declared version is caught, and the same tag
# passes once the declaration is corrected back to the real value.
realrepo="$TMP/realrepo"
mkdir -p "$realrepo"
cp "$REPO_ROOT/pyproject.toml" "$realrepo/pyproject.toml"
cat > "$realrepo/.version.yaml" <<'EOF'
schema: 1
source_of_truth: pyproject.toml
locations:
  - path: pyproject.toml
    pattern: '^version\s*=\s*"(\d+\.\d+\.\d+)"'
    required: true
EOF

real_version="$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\([0-9.]*\)"/\1/p' "$REPO_ROOT/pyproject.toml")"
real_tag="v$real_version"

if [[ "$real_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    # Real tag, real content: gate green once, as shipped.
    if gate "$realrepo" "$real_tag"; then
        ok "real case — real tag $real_tag against real pyproject.toml -> green"
    else
        bad "real case — real tag $real_tag against real pyproject.toml -> expected green"
    fi

    # Deliberately mismatch the declared version; same real tag must turn red.
    # BSD sed has no \s; match the literal bytes of the shipped pyproject header.
    sed -i'.bak' 's/^version = "[0-9.]*"/version = "2.0.0"/' "$realrepo/pyproject.toml"
    if gate "$realrepo" "$real_tag"; then
        bad "real case — mismatched declared version -> expected gate red"
    else
        ok "real case — mismatched declared version (2.0.0 vs $real_tag) -> gate red"
    fi

    # Correct it back; the same tag passes again.
    sed -i'.bak' "s/^version = \"[0-9.]*\"/version = \"$real_version\"/" "$realrepo/pyproject.toml"
    if gate "$realrepo" "$real_tag"; then
        ok "real case — same tag $real_tag after correction -> green"
    else
        bad "real case — same tag $real_tag after correction -> expected green"
    fi
else
    bad "real case — expected pyproject.toml to declare a plain semver, got ${real_version:-none}"
fi
rm -f "$realrepo/pyproject.toml.bak"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
