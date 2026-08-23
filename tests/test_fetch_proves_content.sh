#!/usr/bin/env bash
# FFR-300-2 — a fetch of version-sync.py must parse as Python before use.
#
# Proves the failure mode documented in docs/version-sync.md: the tool once
# lived behind a private ops boundary and was fetched at runtime by release
# gates. The fetch got an HTML login page served with HTTP 200 and reported
# success, so the gate died on a syntax error later. The guard here parses the
# download as Python before use and, on failure, names what actually arrived.
#
# Checks (criterion 2 + 3):
#   A. The old private URL yields HTML and is rejected by the guard.
#   B. The public URL yields a shebang and is accepted by the guard.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

OLD_PRIVATE_URL="https://git.langevc.com/langevc/lvc-ops/raw/branch/main/scripts/version-sync.py"
PUBLIC_URL="https://raw.githubusercontent.com/LangeVC/ops-engine/master/scripts/version-sync.py"

TMPDIR_="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_"' EXIT

failures=0
fail() {
    echo "FAIL: $1"
    failures=$((failures + 1))
}

# fetch_and_prove_python URL DEST LABEL
#
# Downloads URL to DEST and parses it as Python before use. On failure it
# names what actually arrived (the download's first line) rather than treating
# a completed transfer as success. HTTP status is deliberately NOT treated as
# proof: the historical bug was an HTML login page served with HTTP 200.
fetch_and_prove_python() {
    local url="$1" dest="$2" label="$3"
    curl -sSL "$url" -o "$dest" || {
        echo "ERROR: $label could not be fetched" >&2
        return 1
    }
    if ! python3 -m py_compile "$dest" 2>/dev/null; then
        echo "ERROR: $label did not parse as Python; what arrived: $(head -n 1 "$dest")" >&2
        return 1
    fi
    return 0
}

# --- A: the old private URL yields HTML and is rejected ---------------------
old_body="$TMPDIR_/old_fetch"
if fetch_and_prove_python "$OLD_PRIVATE_URL" "$old_body" "old private fetch"; then
    fail "old private URL was accepted — expected an HTML login page to be rejected"
fi
head -n 1 "$old_body" | grep -qiE '<!DOCTYPE html>|<html' \
    || fail "old private URL did not yield HTML (first line: $(head -n 1 "$old_body"))"

# --- B: the public URL yields a shebang and is accepted ---------------------
pub_body="$TMPDIR_/pub_fetch"
fetch_and_prove_python "$PUBLIC_URL" "$pub_body" "public fetch" \
    || fail "public URL was rejected — expected the download to parse as Python"
head -n 1 "$pub_body" | grep -qE '^#!' \
    || fail "public URL did not yield a shebang (first line: $(head -n 1 "$pub_body"))"

if (( failures > 0 )); then
    echo "test_fetch_proves_content: FAIL ($failures)"
    exit 1
fi
echo "test_fetch_proves_content: PASS"
