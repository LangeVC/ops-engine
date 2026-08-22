#!/usr/bin/env bash
# FFR-300-1 — the generic template's release gate is config-driven and carries
# no hard-coded org, host or product name.
#
# Checks (criterion 1 + 2):
#   A. The gate reads its expected release-name prefix from configuration.
#   B. The engine's own name ("Ops Engine") appears as this repo's config value,
#      not as a literal in the workflow.
#   C. The config value composes a valid "<prefix> vX.Y.Z" regex.
#   D. No known LangeVC org/host/product token is hard-coded anywhere in the
#      generic template's gate surface (.github/) other than the config file
#      that legitimately declares the engine's own name.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CONFIG=".github/release-name.yml"
WORKFLOW=".github/workflows/enforce-release-name.yml"

failures=0
fail() {
    echo "FAIL: $1"
    failures=$((failures + 1))
}

# --- A: the gate reads its prefix from configuration -----------------------
prefix=""
if [[ -f "$CONFIG" ]]; then
    prefix=$(sed -n 's/^release_name_prefix:[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}[[:space:]]*$/\1/p' "$CONFIG")
fi
[[ -f "$CONFIG" ]] || fail "config $CONFIG does not exist"
[[ -n "$prefix" ]] || fail "config $CONFIG does not carry a release_name_prefix"
grep -q 'release-name.yml' "$WORKFLOW" \
    || fail "gate $WORKFLOW does not reference the config file"

# --- B: the engine's own name is config data, not a workflow literal --------
# The workflow's run: block must not contain the hard-coded product name.
if grep -q 'Ops Engine' "$WORKFLOW"; then
    fail "'Ops Engine' is still hard-coded in $WORKFLOW"
fi
if grep -qv 'release_name_prefix' <<<"$prefix" && [[ "$prefix" != "Ops Engine" ]]; then
    fail "unexpected prefix '$prefix' in config"
fi

# --- C: the config prefix composes a valid <prefix> vX.Y.Z regex -------------
regex="^${prefix}[[:space:]]v[0-9]+\.[0-9]+\.[0-9]+.*$"
if ! [[ "Ops Engine v2.0.0" =~ $regex ]]; then
    fail "config prefix '$prefix' does not match '<prefix> vX.Y.Z'"
fi
if [[ "Ops Engine 2.0.0" =~ $regex ]]; then
    fail "config-derived regex wrongly accepted a release without the 'v' prefix"
fi

# --- D: no org/host/product token hard-coded outside the config file ---------
# Known business tokens measured in the LVC-222 audit / this finding.
tokens=( "langevc" "LangeVC" "fusionaize" "fusionAIze" "capacium" "elementeer" \
         "git.langevc.com" "github.com/LangeVC" "faigrid" "faigate" "Ops Engine" )
mapfile -t template_files < <(git ls-files '.github/')  || true
(( ${#template_files[@]} > 0 )) || fail "no tracked files under .github/"

for token in "${tokens[@]}"; do
    for f in "${template_files[@]}"; do
        # The config file legitimately declares the engine's own name.
        [[ "$f" == "$CONFIG" ]] && continue
        if grep -qiF "$token" "$f"; then
            fail "org/host/product token '$token' hard-coded in tracked template file $f"
        fi
    done
done

if (( failures > 0 )); then
    echo "test_template_has_no_business_names: FAIL ($failures)"
    exit 1
fi
echo "test_template_has_no_business_names: PASS"
