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

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
