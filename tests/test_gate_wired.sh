#!/usr/bin/env bash
# FFR-600-9 — the release gate is wired on main and on dev through one code
# path, not a copy.
#
# Runs under bash -eo pipefail, the same shell and flags as the runner.
#
# Checks:
#   1. The workflow .forgejo/workflows/release-gate.yml exists, triggers on v*
#      tags, and invokes scripts/version-sync.py check-tag exactly once.
#   2. check-tag is invoked by exactly one workflow file — main and dev share
#      the gate, they do not each carry a copy.
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

# --- 2. one code path, not a copy -------------------------------------------
gate_files=()
while IFS= read -r f; do
    [[ -n "$f" ]] && gate_files+=("$f")
done < <(grep -rl 'check-tag' .forgejo/workflows/ 2>/dev/null || true)
if [[ ${#gate_files[@]} -eq 1 && "${gate_files[0]}" == "$WORKFLOW" ]]; then
    ok "check-tag is invoked by exactly one workflow ($WORKFLOW)"
else
    bad "check-tag must be invoked by exactly $WORKFLOW, found: ${gate_files[*]:-none}"
fi

n_invocations=$(grep -c 'check-tag' "$WORKFLOW")
if [[ "$n_invocations" -eq 1 ]]; then
    ok "check-tag appears once in the workflow (single code path)"
else
    bad "check-tag appears $n_invocations times; expected one code path, not a copy"
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

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
