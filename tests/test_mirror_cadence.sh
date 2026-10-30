#!/usr/bin/env bash
# LNF-300-1 / LVC-206 — mirror cadence is a rule, not a habit.
#
# Runs under bash -eo pipefail, the same shell and flags as the runner.
#
# Checks:
#   1. The cadence rule is stated once in docs/mirror-cadence.md, and 'cadence'
#      is binding there (the precondition: before this task, grep for 'cadence'
#      returned nothing binding).
#   2. The mechanics (scripts/mirror-cadence-check.py) and the policy (the
#      declaration the layover holds) are split: the check ships here in
#      ops-engine, the deviation decision stays in the declaration.
#   3. Red proof: a mirrored repository whose drift window deviates from the
#      default and carries no named exception is reported (non-zero); the same
#      deviation covered by a named exception is quiet.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DOC="docs/mirror-cadence.md"
CHECK="scripts/mirror-cadence-check.py"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass=0
fail=0
ok()  { printf 'PASS: %s\n' "$1"; pass=$((pass+1)); }
bad() { printf 'FAIL: %s\n' "$1"; fail=$((fail+1)); }

# --- 1. the rule is stated once, and binding --------------------------------
[[ -f "$DOC" ]] || bad "cadence document $DOC does not exist"
[[ -f "$CHECK" ]] || bad "cadence check $CHECK does not exist"

# 'cadence' must actually occur in the document as the rule, not merely as a
# filename. The whole reason this lane exists is that grep previously returned
# nothing binding.
cadence_hits="$(grep -c 'cadence' "$DOC" || true)"
if [[ "$cadence_hits" -ge 1 ]]; then
    ok "cadence is stated in $DOC (${cadence_hits} occurrence(s))"
else
    bad "grep for 'cadence' in $DOC returns nothing binding"
fi

# The check must not be a copy of another repo's check: it lives in ops-engine,
# and the policy stays in the declaration. It carries no org/product name.
if grep -qE 'LangeVC|fusionAIze|[Cc]apacium|[Ss]kill[Ww]eave|[Ee]lementeer' "$CHECK"; then
    bad "the cadence check carries an org/product name; mechanics must stay generic"
else
    ok "mechanics stay generic (no org/product name in the check)"
fi

# --- 3. red proof: deviation reported, exception quiet -----------------------
make_doc() {
    local file="$1" default="$2" drift="$3" has_exception="$4"
    local exc_block=""
    if [[ "$has_exception" == "yes" ]]; then
        exc_block=',
            "exception": {
              "reason": "operational: mirror runs behind a rate-limited forge",
              "decided_by": "operator",
              "value": "600"
            }'
    fi
    cat > "$file" <<EOF
The mirror cadence is stated once. See the rule below.

Rule: a mirrored repository follows the default drift window unless it carries
a named exception.

\`\`\`json
{
  "schema": 1,
  "default_drift_seconds": $default,
  "mirrors": [
    {
      "org": "test",
      "repo": "demo",
      "drift_seconds": $drift${exc_block}
    }
  ]
}
\`\`\`
EOF
}

# A deviating repo with no exception must be reported (check exits non-zero).
make_doc "$TMP/dev.md" 300 900 no
if python3 "$CHECK" --doc "$TMP/dev.md"; then
    bad "red proof — deviation without exception -> expected non-zero"
else
    ok "red proof — deviation without exception is reported"
fi

dev_report="$(python3 "$CHECK" --doc "$TMP/dev.md" 2>&1 || true)"
if printf '%s' "$dev_report" | grep -q 'test/demo'; then
    ok "red proof — the report names the deviating repository (test/demo)"
else
    bad "red proof — the report does not name the deviating repository"
fi

# The same deviation, covered by a named exception, must be quiet (exit 0).
make_doc "$TMP/exc.md" 300 900 yes
if python3 "$CHECK" --doc "$TMP/exc.md"; then
    ok "red proof — deviation covered by a named exception is quiet"
else
    bad "red proof — named exception -> expected quiet (exit 0)"
fi

# A repository that follows the default must be quiet too.
make_doc "$TMP/ok.md" 300 300 no
if python3 "$CHECK" --doc "$TMP/ok.md"; then
    ok "a repository at the default cadence is quiet"
else
    bad "a repository at the default cadence -> expected quiet"
fi

# --- 4. the in-tree declaration itself is compliant --------------------------
if python3 "$CHECK" --doc "$DOC"; then
    ok "the in-tree declaration follows the rule (no un-excepted deviation)"
else
    bad "the in-tree declaration has an un-excepted cadence deviation"
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
