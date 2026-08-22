#!/usr/bin/env bash
# FFR-300-1 — the generic template's release gate is config-driven and carries
# no hard-coded org, host or product name. Criterion 3 generalises the scan: it
# runs as a check across the three open templates (ops-engine, agent-test-env,
# wp-test-env) and in both directions.
#
# Checks (criterion 1 + 2):
#   A. The gate reads its expected release-name prefix from configuration.
#   B. The engine's own name ("Ops Engine") appears as this repo's config value,
#      not as a literal in the workflow.
#   C. The config value composes a valid "<prefix> vX.Y.Z" regex.
#   D. No known LangeVC org/host/product token is hard-coded anywhere in the
#      generic template's gate surface (.github/) other than the config file
#      that legitimately declares the engine's own name.
#
# Checks (criterion 3 — the scan across the three open templates):
#   E. FORWARD direction — in a release/CI gate's run surface (workflow `run:` or
#      `env:` and its default config), every org/host/product name is either
#      declared as config data or is the repository's own identity (its mirror
#      URL, its own image distribution ref). A bare, unconfigurable product or
#      org token in a gate default is reported as a leak.
#   F. REVERSE direction — a gate must not duplicate generic logic that already
#      lives in the base, and must not resolve generic tooling from a private
#      layover. Any generic script copied into a template from a layover is
#      reported so the base does not starve while copies multiply.
#
# Templates are scanned via $FFR_TEMPLATES (colon-separated paths). Default: the
# current repository only. When sibling template checkouts are provided, each is
# scanned and a leak in any one of them fails the run.
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

# Known business tokens measured in the LVC-222 audit / this finding. These are
# the org, host and product names the generic OSS templates must not leak into a
# consumer's runtime default.
tokens=( "langevc" "LangeVC" "fusionaize" "fusionAIze" "capacium" "elementeer" \
         "git.langevc.com" "github.com/LangeVC" "faigrid" "faigate" "Ops Engine" )

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

# --- E: FORWARD direction — scan the release/CI gate run surface -------------
# A template's own distribution identity (its mirror remote, its own container
# image ref) legitimately carries the org name. A bare product or org token
# sitting in a gate's `run:` or `env:` block that the consumer cannot override
# is a leak: business identity written as template data, exactly the class that
# FFR-300-1 fixes in the release gate.
forward_scan() {
    local root="$1" token g in_block line
    # Release/CI workflow files under the template (not the Forgejo mirror, whose
    # whole purpose is the repository's own distribution remote).
    local gate_files=()
    while IFS= read -r g; do
        [[ -n "$g" ]] && gate_files+=( "$g" )
    done < <(git -C "$root" ls-files '.github/workflows/*.yml' '.github/workflows/*.yaml' 2>/dev/null || true)
    (( ${#gate_files[@]} > 0 )) || { fail "no tracked .github/workflows in template '$root'"; return 0; }

    for g in "${gate_files[@]}"; do
        local gate_path="$root/$g"
        [[ -f "$gate_path" ]] || continue
        # We only judge `run:` / `env:` blocks — the executable surface a
        # consumer inherits wholesale. Walk every line so block boundaries are
        # seen, not just token-bearing lines.
        for token in "${tokens[@]}"; do
            in_block=0
            while IFS= read -r line; do
                if [[ "$line" =~ ^[[:space:]]*(-[[:space:]]+)?(run|env):[[:space:]]*(\||>)?[[:space:]]*$ ]]; then
                    in_block=1
                    continue
                fi
                # A line that is itself a new bottom-level mapping key (before
                # the block scalar's indented body returns) closes the block.
                if [[ "$line" =~ ^[[:space:]]*(-[[:space:]]+)?[a-zA-Z0-9_-]+:[[:space:]]*$ ]] && (( in_block )); then
                    in_block=0
                fi
                if (( in_block )) && grep -qiF "$token" <<<"$line"; then
                    fail "FORWARD leak: token '$token' hard-coded in gate run:/env: block ($root/$g)"
                    in_block=0
                fi
            done < "$gate_path"
        done
    done
}
forward_scan "$REPO_ROOT"

# --- F: REVERSE direction — no duplicated or privately sourced generic logic --
# The base must not starve while copies multiply: a template gate must not carry
# a second copy of generic tooling that already lives in the base, nor resolve
# it from a private layover location (reached only with a credential). A generic
# script path fetched from a private org repository is reported.
reverse_scan() {
    local root="$1" g ref
    local gate_files=()
    while IFS= read -r g; do
        [[ -n "$g" ]] && gate_files+=( "$g" )
    done < <(git -C "$root" ls-files '.github/workflows/*.yml' '.github/workflows/*.yaml' 2>/dev/null || true)

    for g in "${gate_files[@]}"; do
        local gate_path="$root/$g"
        [[ -f "$gate_path" ]] || continue
        while IFS= read -r ref; do
            [[ -n "$ref" ]] || continue
            fail "REVERSE finding: gate resolves generic tooling from a private location ($root/$g): $ref"
        done < <(grep -nE 'gitlab|lvc-ops|capacium-ops|fusionaize-ops|elementeer-ops|skillweave-ops|/user/login|raw.*303' "$gate_path" 2>/dev/null || true)
    done
}
reverse_scan "$REPO_ROOT"

# --- Multi-template sweep: when sibling template checkouts are supplied -------
if [[ -n "${FFR_TEMPLATES:-}" ]]; then
    IFS=':' read -r -a template_roots <<< "$FFR_TEMPLATES"
    for t in "${template_roots[@]}"; do
        [[ -n "$t" ]] || continue
        [[ -d "$t" ]] || { fail "template path '$t' is not a directory"; continue; }
        forward_scan "$t"
        reverse_scan "$t"
    done
fi

if (( failures > 0 )); then
    echo "test_template_has_no_business_names: FAIL ($failures)"
    exit 1
fi
echo "test_template_has_no_business_names: PASS"
