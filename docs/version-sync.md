# version-sync

`scripts/version-sync.py` is the single source of truth for a repository's
version locations. It is a generic, single-file tool that lives here, in the
open base, and carries no organisation, host or product name — anyone can use
it unchanged.

## What problem it solves

A version bump must hit **every** place a repository's version appears. Those
places differ per repository and are sometimes untypical to parse:

- a WordPress plugin header (`* Version: X.Y.Z`)
- a PHP constant (`define( 'X_VERSION', 'X.Y.Z' )`)
- a JSON field (`"version": "X.Y.Z"`)
- a YAML key (`version: X.Y.Z`)
- a TOML field (`version = "X.Y.Z"`)
- plain text (a `VERSION` file, possibly `v`-prefixed)
- a Homebrew formula URL (`/tags/vX.Y.Z.tar.gz`)
- a WordPress `Stable tag:` line

A scan for one of these shapes misses the rest. So each repository declares
explicitly where its versions live — and the tool that checks and the tool
that bumps read the same declaration.

## Core idea: declaration, not source

External readers (npm, composer, WordPress, Homebrew) each read their own
file, so "one source the rest derives from" cannot work — the literal values
must still appear in every file, which only moves the sync problem up a
level. The single truth is established elsewhere: the bump tool and the
release gate read **the same declaration**. The declaration lists the
locations; the tool checks and bumps against that list. The declaration
cannot drift against the gate because it *is* the gate.

## The declaration file

Each repository carries one machine-readable file at its root:
**`.version.yaml`**.

```yaml
# .version.yaml — declares this repository's version locations.
schema: 1

# The location whose value is canonical for this repository. Exactly one
# reference must be binding; `source_of_truth` points at it.
source_of_truth: package.json

locations:
  - path: package.json                             # relative to the repo root
    pattern: '"version":\s*"(\d+\.\d+\.\d+)'       # regex; capture group 1 = version
    required: true                                 # binding: bump writes, gate fails on drift
  - path: capability.yaml
    pattern: '^version:\s*(\d+\.\d+\.\d+)'
    required: true
  - path: readme.txt
    pattern: '^Stable tag:\s*(\d+\.\d+\.\d+)'
    required: false                                # informational: gate warns, bump takes along
```

### Fields

| Field | Meaning |
|---|---|
| `schema` | Version of this schema (`1`). The reader rejects anything unknown. |
| `source_of_truth` | Path of the canonical location. Must be a `required: true` location. |
| `locations[]` | The list of version locations. |
| `locations[].path` | Path relative to the repository root. |
| `locations[].pattern` | Regex. **Must** contain exactly one capture group, which yields the version. |
| `locations[].required` | `true` = binding (bump writes, gate fails on drift). `false` = informational (gate warns, bump writes only with `--include-informational`). |

### Rules

1. `source_of_truth` must point at a `required: true` location.
2. Every location must yield exactly one version capture group. A pattern
   that matches nothing (or matches but lacks the group) is a hard error,
   reported loudly — never silent.
3. `false` locations document explicitly "not automatically maintained"
   rather than surfacing again as errors two months from now.
4. Places that carry a version but are never bumped by this release line
   (prose, a changelog, a sibling package with its own version) do **not**
   belong in the list. Record the reasoning in a `notes` comment.

## Usage

```text
version-sync.py check [--warnings-as-errors] [--repo PATH]
version-sync.py check-tag TAG [--warnings-as-errors] [--repo PATH]
version-sync.py bump NEW_VERSION [--include-informational] [--repo PATH]
```

`--repo` defaults to the current directory.

### `check` — the release gate

Verifies that every `required` location equals the `source_of_truth`. Each
`false` location that disagrees produces a warning. Exit code 0 only when all
binding locations agree.

### `check-tag TAG` — the tag-vs-location gate

Runs `check`, then verifies the `source_of_truth` equals `TAG` (a `v` prefix
is stripped). A tag can point at a commit whose declared version lags behind;
`check` would pass that, so this mode closes the gap.

### `bump NEW_VERSION` — the bump tool

Writes the new version into every `required` location (and, with
`--include-informational`, into `false` locations), then re-runs `check` to
self-verify. A bump that misses a location fails the gate in the same run. A
pattern that matches nothing refuses to write rather than produce a
half-bumped file.

## The parser is the contract

The declared subset is deliberately small: 2-space-indented maps, `-` list
items, `schema:` integer, `source_of_truth:` and `path:` scalars, a quoted
`pattern:`, and a `required:` boolean. Anything else raises. A declaration
that cannot be stated in this subset should not exist silently.

## Implementation notes

- Stdlib only (`argparse`, `re`, `pathlib`), no external dependencies, so it
  runs on an empty `ubuntu-latest` Forgejo runner.
- The gate and the bump tool call the same parser: the declaration cannot
  drift against its own gate.
- This is the canonical home. The tool previously lived behind a private ops
  boundary and was fetched at runtime by release gates in other
  organisations; a fetch that got an HTML login page with HTTP 200 reported
  success and the gate died on a syntax error later. Here it is reachable
  from the public mirror without a credential. Do not fork a second copy.
