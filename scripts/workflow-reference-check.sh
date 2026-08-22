#!/usr/bin/env bash
# workflow-reference-check — fail on push when a workflow references a path that does not exist.
#
# The runner shell is bash -eo pipefail. This script runs under exactly that
# shell and flag set (see `set -euo pipefail` below), so a missing reference
# aborts with a non-zero exit and the push is rejected.
#
# Usage:
#     workflow-reference-check.sh [WORKFLOW ...]
#
# With no arguments, scan every workflow under .forgejo/workflows/ and
# .github/workflows/. With one or more arguments, check only those files.
#
# Exit codes:
#     0  every referenced path exists
#     1  at least one workflow references a path that does not exist
#     2  usage / invocation error

set -euo pipefail

die() {
    printf 'workflow-reference-check: ERROR: %s\n' "$*" >&2
    exit 2
}

# A referenced path is a `run:` or `uses:` value that points at a file on
# disk (starts with `.` or a relative non-command token). We only flag local
# file references; command names (e.g. `git`) and `actions/checkout@v4`
# strings are external and carry no local-path guarantee.
extract_paths() {
    local file="$1"
    sed -nE 's/^[[:space:]]*-?[[:space:]]*(run|uses):[[:space:]]+["'"'"']?([^"'"'"'#[:space:]][^"'"'"'#]*).*/\2/p' "$file"
}

check_workflow() {
    local workflow="$1"
    local path missing=0

    [[ -f "$workflow" ]] || die "workflow not found: $workflow"

    while IFS= read -r path; do
        [[ -n "$path" ]] || continue

        # Ignore external actions (owner/repo@ref) and bare commands.
        case "$path" in
            */*"@"* | [A-Za-z0-9_./-]*'@'*) continue ;;   # external action / command w/ version
        esac

        # A local reference starts with `./` or `../`, or is a bare relative
        # script path enclosed with a shell invocation (e.g. `bash scripts/x`).
        local candidate="${path#* }"   # strip a leading interpreter+space if present
        case "$candidate" in
            ./*|../*) ;;
            *) continue ;;
        esac

        # Strip shell metachars and trailing arguments down to a path.
        candidate="${candidate%% *}"
        candidate="${candidate%%&*}"

        if [[ ! -e "$candidate" ]]; then
            printf 'workflow-reference-check: %s references missing path: %s\n' \
                "$workflow" "$candidate" >&2
            missing=1
        fi
    done < <(extract_paths "$workflow")

    return "$missing"
}

main() {
    local workflows=()
    local workflow
    local rc=0

    if [[ $# -gt 0 ]]; then
        workflows=("$@")
    else
        local dir
        for dir in .forgejo/workflows .github/workflows; do
            [[ -d "$dir" ]] || continue
            while IFS= read -r -d '' workflow; do
                workflows+=("$workflow")
            done < <(find "$dir" -maxdepth 1 -type f '(' -name '*.yml' -o -name '*.yaml' ')' -print0)
        done
    fi

    [[ ${#workflows[@]} -gt 0 ]] || die "no workflows to check"

    for workflow in "${workflows[@]}"; do
        check_workflow "$workflow" || rc=1
    done

    if [[ "$rc" -eq 0 ]]; then
        printf 'workflow-reference-check: all referenced paths exist\n'
    fi

    exit "$rc"
}

main "$@"
