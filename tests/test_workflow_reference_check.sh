#!/usr/bin/env bash
# Test suite for scripts/workflow-reference-check.sh.
#
# Runs under bash -eo pipefail, the same shell and flags as the runner and as
# the script under test, so the very shell contract is part of the proof.

set -euo pipefail

SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/workflow-reference-check.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass=0
fail=0

ok()   { printf 'PASS: %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf 'FAIL: %s\n' "$1"; fail=$((fail+1)); }

run() {
    if "$@" >"$TMP/out" 2>"$TMP/err"; then
        return 0
    else
        return 1
    fi
}

# 1 — a workflow whose referenced file exists passes
mkdir -p "$TMP/existing"
printf '#!/usr/bin/env bash\necho hi\n' > "$TMP/existing/tool.sh"
cat > "$TMP/existing/workflow.yml" <<EOF
name: ref-ok
on: push
jobs:
  job1:
    runs-on: ubuntu-latest
    steps:
      - run: bash ./tool.sh
EOF
if (cd "$TMP/existing" && run bash "$SCRIPT" workflow.yml); then
    ok "referenced path exists -> exits 0"
else
    bad "referenced path exists -> expected exit 0"
fi

# 2 — a workflow referencing a missing path fails (non-zero)
mkdir -p "$TMP/missing"
cat > "$TMP/missing/workflow.yml" <<EOF
name: ref-missing
on: push
jobs:
  job1:
    runs-on: ubuntu-latest
    steps:
      - run: bash ./nonexistent-script.sh
EOF
if (cd "$TMP/missing" && run bash "$SCRIPT" workflow.yml); then
    bad "missing path -> expected non-zero exit"
else
    if grep -q "nonexistent-script.sh" "$TMP/err"; then
        ok "missing path -> non-zero exit and names the missing path"
    else
        bad "missing path -> error output does not name the path"
    fi
fi

# 3 — script is executable and carries the bash -eo pipefail contract
if head -n 1 "$SCRIPT" | grep -q '#!/usr/bin/env bash'; then
    ok "shebang is bash"
else
    bad "shebang is not bash"
fi
if grep -q 'set -euo pipefail' "$SCRIPT"; then
    ok "script sets -euo pipefail"
else
    bad "script does not set -euo pipefail"
fi

# 4 — red proof: removing a referenced script turns the next push red before
# any release. The reference check is the push-time gate; a release step runs
# only after the gate passes, mirroring push -> gate -> release. The red must
# appear at the gate, so the release step is never reached.
mkdir -p "$TMP/redproof"
printf '#!/usr/bin/env bash\necho deploy\n' > "$TMP/redproof/deploy.sh"
cat > "$TMP/redproof/workflow.yml" <<'EOF'
name: red-proof
on: push
jobs:
  job1:
    runs-on: ubuntu-latest
    steps:
      - run: bash ./deploy.sh
EOF

push_gate_then_release() {
    if run bash "$SCRIPT" workflow.yml; then
        touch release-reached.marker
        return 0
    fi
    return 1
}

# 4a — green first: the referenced script exists, so the first push passes and
# the release step is reached.
if (cd "$TMP/redproof" && push_gate_then_release); then
    ok "red proof — referenced script present -> push green"
    if [[ -e "$TMP/redproof/release-reached.marker" ]]; then
        ok "red proof — green gate reaches release"
    else
        bad "red proof — green gate did not reach release"
    fi
else
    bad "red proof — referenced script present -> expected push green"
fi

# 4b — red: remove the referenced script; the next push turns red and the
# release step is never reached.
rm "$TMP/redproof/deploy.sh"
rm -f "$TMP/redproof/release-reached.marker"
if (cd "$TMP/redproof" && push_gate_then_release); then
    bad "red proof — removed script -> expected next push red"
else
    if grep -q "deploy.sh" "$TMP/err"; then
        ok "red proof — removed script -> next push red, names the missing script"
    else
        bad "red proof — red gate does not name the removed script"
    fi
fi
if [[ -e "$TMP/redproof/release-reached.marker" ]]; then
    bad "red proof — release step ran despite the red gate"
else
    ok "red proof — red gate fired before any release step"
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
