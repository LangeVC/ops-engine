#!/usr/bin/env bash
# FFR-400-2 — every layover resolves ops-engine from the Forgejo canonical,
# and reachability of the canonical host is proven, not assumed.
#
# Two claims, two checks:
#   A. The canonical host resolves the pinned tag to the same commit the
#      GitHub mirror carries. A tag that differs is a mirror lag, not parity.
#   B. The canonical host is reachable over anonymous HTTPS (the deploy-host
#      build path — `pip install git+https://...`) and over SSH (the runner
#      path) — a full clone and an ls-remote, not a HEAD request.
#
# Runs under bash -eo pipefail, the same shell as the runner and as the
# deploy-host build, so the reachability it proves is the reachability the
# consumers actually get.
set -euo pipefail

CANONICAL_HOST="git.langevc.com"
CANONICAL_REPO="langevc/ops-engine"
CANONICAL_HTTPS="https://${CANONICAL_HOST}/${CANONICAL_REPO}.git"
CANONICAL_SSH="git@${CANONICAL_HOST}:${CANONICAL_REPO}.git"
MIRROR_HTTPS="https://github.com/LangeVC/ops-engine.git"
PINNED_TAG="v3.0.0"

failures=0
fail() {
    echo "FAIL: $1"
    failures=$((failures + 1))
}

# A — the pinned tag resolves to the same commit on canonical and mirror.
#
# The tag on the canonical is annotated (`v3.0.0` -> commit), the tag on the
# mirror is lightweight. Dereference the annotated form first (`^{}`) and fall
# back to the bare tag, so a tag object and a tag pointing straight at a commit
# both resolve to the commit the pin actually installs.
resolve_tag_commit() {
    local url="$1" sha
    sha="$(git ls-remote "$url" "refs/tags/${PINNED_TAG}^{}" 2>/dev/null | awk '{print $1}')"
    if [[ -z "$sha" ]]; then
        sha="$(git ls-remote "$url" "refs/tags/${PINNED_TAG}" 2>/dev/null | awk '{print $1}')"
    fi
    printf '%s' "$sha"
}
canonical_commit="$(resolve_tag_commit "$CANONICAL_HTTPS")"
mirror_commit="$(resolve_tag_commit "$MIRROR_HTTPS")"

if [[ -z "$canonical_commit" ]]; then
    fail "canonical $CANONICAL_HTTPS did not resolve tag ${PINNED_TAG}"
elif [[ -z "$mirror_commit" ]]; then
    fail "mirror $MIRROR_HTTPS did not resolve tag ${PINNED_TAG}"
elif [[ "$canonical_commit" != "$mirror_commit" ]]; then
    fail "canonical and mirror disagree on ${PINNED_TAG}: $canonical_commit vs $mirror_commit"
else
    echo "PASS: ${PINNED_TAG} resolves to ${canonical_commit} on canonical and mirror"
fi

# B — reachability proven per path, not assumed from a HEAD request.
#
# The deploy host resolves ops-engine through a git+https dependency, which
# runs a real clone against the tag. Prove that clone completes and that its
# HEAD is the pinned commit — a full transfer, not a status page.
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

if git clone --quiet --depth 1 --branch "$PINNED_TAG" "$CANONICAL_HTTPS" "$tmpdir/clone" 2>/dev/null; then
    cloned_commit="$(git -C "$tmpdir/clone" rev-parse HEAD 2>/dev/null)"
    if [[ "$cloned_commit" == "$canonical_commit" ]]; then
        echo "PASS: anonymous HTTPS clone of canonical resolves ${PINNED_TAG} to ${cloned_commit}"
    else
        fail "canonical clone HEAD $cloned_commit != pinned commit $canonical_commit"
    fi
else
    fail "anonymous HTTPS clone of canonical failed (deploy-host path unreachable)"
fi

if ssh_sha="$(resolve_tag_commit "$CANONICAL_SSH")"; then
    if [[ -n "$ssh_sha" && "$ssh_sha" == "$canonical_commit" ]]; then
        echo "PASS: SSH ls-remote of canonical resolves ${PINNED_TAG} to ${ssh_sha}"
    else
        fail "SSH ls-remote of canonical did not resolve ${PINNED_TAG} to the pinned commit"
    fi
else
    fail "SSH ls-remote of canonical failed (runner path unreachable)"
fi

if (( failures > 0 )); then
    echo "test_canonical_source: FAIL ($failures)"
    exit 1
fi
echo "test_canonical_source: PASS"
