#!/usr/bin/env python3
"""version-sync — single source of truth for a repo's version locations.

Reads a `.version.yaml` declaration (see docs/versionsorte-deklaration.md) and
either *checks* (gate mode, for the release gate) or *bumps* (bump mode, for
the bump tool) every declared location. The gate and the bump tool call the
same parser, so the declaration cannot drift against the gate: it IS the gate.

Stdlib only, no external deps, so it runs on an empty `ubuntu-latest` runner.

Usage:
    version-sync.py check [--warnings-as-errors] [--repo PATH]
    version-sync.py check-tag TAG [--warnings-as-errors] [--allow-prereleases] [--repo PATH]
    version-sync.py bump NEW_VERSION [--include-informational] [--repo PATH]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import NoReturn

SCHEMA = 1

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
PRERELEASE_RE = re.compile(r"^\d+\.\d+\.\d+-[0-9A-Za-z.-]+$")


class PrereleaseNotAllowedError(Exception):
    """A prerelease tag or version was gated off by policy."""


def _is_prerelease(version: str) -> bool:
    return PRERELEASE_RE.match(version) is not None


def _die(msg: str) -> "NoReturn":
    print(f"version-sync: ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def parse_decl(text: str) -> dict:
    """Parse the minimal YAML subset `.version.yaml` uses.

    The declaration is restricted on purpose: 2-space indented maps, `-` list
    items, `schema:` int, `source_of_truth:` and `path:` scalars, `pattern:`
    quoted string, `required:` bool. Anything else raises; a declaration that
    cannot be represented in this subset should not exist silently.
    """
    lines = [ln.rstrip("\n") for ln in text.splitlines()]
    decl: dict = {"locations": []}
    cur: dict | None = None

    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = raw.strip()
        if stripped.startswith("-"):
            if cur is not None:
                decl["locations"].append(cur)
            cur = {"required": True}
            key, _, val = stripped[1:].strip().partition(":")
            if key and val.strip():
                cur[key.strip()] = val.strip()
        elif raw.startswith("  ") or raw.startswith("\t"):
            if cur is None:
                _die("list item attribute before any '-' item")
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip().strip("'\"").strip()
            if key == "required":
                cur["required"] = val.lower() in ("true", "yes", "1")
            else:
                cur[key] = val
        else:
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()
            if key == "locations":
                continue  # list follows; keep the pre-initialized list
            decl[key] = int(val) if key == "schema" else val.strip("'\"").strip()

    if cur is not None:
        decl["locations"].append(cur)
    return decl


def validate_decl(decl: dict) -> None:
    if decl.get("schema") != SCHEMA:
        _die(f"unsupported schema {decl.get('schema')!r}; expected {SCHEMA}")
    sot = decl.get("source_of_truth")
    if not sot:
        _die("missing 'source_of_truth'")
    locs = decl.get("locations")
    if not locs:
        _die("no 'locations' declared")

    sot_found = False
    for loc in locs:
        if "path" not in loc or "pattern" not in loc:
            _die(f"location missing path/pattern: {loc!r}")
        rx = re.compile(loc["pattern"])
        if rx.groups != 1:
            _die(
                f"pattern for {loc['path']!r} must have exactly one capture "
                f"group (version), has {rx.groups}"
            )
        if loc["path"] == sot:
            sot_found = True
            if not loc.get("required", True):
                _die(f"source_of_truth {sot!r} must be a required location")
    if not sot_found:
        _die(f"source_of_truth {sot!r} is not among the locations")


def read_version(path: Path, pattern: str) -> str | None:
    if not path.exists():
        return None
    rx = re.compile(pattern)
    text = path.read_text()
    for line in text.splitlines():
        m = rx.search(line)
        if m:
            return m.group(1)
    return None


def load(repo: Path) -> tuple[dict, list[dict]]:
    decl_file = repo / ".version.yaml"
    if not decl_file.exists():
        _die(f"no {decl_file} (missing declaration)")
    decl = parse_decl(decl_file.read_text())
    validate_decl(decl)
    return decl, decl["locations"]


def check(
    repo: Path, warnings_as_errors: bool, allow_prereleases: bool = False
) -> int:
    decl, locs = load(repo)
    sot = decl["source_of_truth"]
    sot_val = read_version(repo / sot, _pattern_for(locs, sot))
    if sot_val is None:
        _die(f"source_of_truth {sot!r} unreadable or pattern did not match")
    if not SEMVER_RE.match(sot_val):
        if not (_is_prerelease(sot_val) and allow_prereleases):
            if _is_prerelease(sot_val):
                raise PrereleaseNotAllowedError(
                    f"source_of_truth {sot!r} value {sot_val!r} is a "
                    f"prerelease; pass --allow-prereleases to permit it"
                )
            _die(f"source_of_truth {sot!r} value {sot_val!r} is not semver")

    print(f"source of truth: {sot} = {sot_val}")
    failed = False
    warned = False
    for loc in locs:
        val = read_version(repo / loc["path"], loc["pattern"])
        tag = "required" if loc.get("required", True) else "informational"
        if val is None:
            print(f"  {'MISSING':11} {loc['path']} ({tag})")
            if loc.get("required", True):
                failed = True
            else:
                warned = True
            continue
        if val != sot_val:
            if loc.get("required", True):
                print(f"  {'MISMATCH':11} {loc['path']} = {val} (want {sot_val}) ({tag})")
                failed = True
            else:
                print(f"  {'mismatch':11} {loc['path']} = {val} (want {sot_val}) ({tag})")
                warned = True
        else:
            print(f"  {'ok':11} {loc['path']} = {val} ({tag})")

    if failed:
        print("version-sync: FAIL — required locations drifted")
        return 1
    if warned and warnings_as_errors:
        print("version-sync: FAIL — informational locations drifted (warnings as errors)")
        return 1
    if warned:
        print("version-sync: informational locations drifted (warning only)")
    print(f"version-sync: OK — all required locations == {sot_val}")
    return 0


def _pattern_for(locs: list[dict], path: str) -> str:
    for loc in locs:
        if loc["path"] == path:
            return loc["pattern"]
    _die(f"no pattern for {path!r}")


def bump(repo: Path, new_version: str, include_informational: bool) -> int:
    if not SEMVER_RE.match(new_version):
        _die(f"new version {new_version!r} is not semver")
    decl, locs = load(repo)

    for loc in locs:
        required = loc.get("required", True)
        if not required and not include_informational:
            print(f"  skip     {loc['path']} (informational)")
            continue
        rx = re.compile(loc["pattern"])
        path = repo / loc["path"]
        if not path.exists():
            _die(f"{loc['path']} does not exist; refusing to create it")
        text = path.read_text()
        lines = text.splitlines(keepends=True)
        replaced = False
        for i, line in enumerate(lines):
            if rx.search(line):
                lines[i] = rx.sub(lambda m: m.group(0).replace(m.group(1), new_version), line)
                replaced = True
        if not replaced:
            _die(
                f"pattern for {loc['path']} matched nothing; not writing a "
                f"half-bumped file"
            )
        path.write_text("".join(lines))
        print(f"  bump     {loc['path']} -> {new_version}")

    print(f"bumped to {new_version}; re-running check to self-verify")
    # Only required locations may fail the self-check. Informational drift
    # (e.g. a readme Stable tag that intentionally lags) is pre-existing and
    # must not make a successful bump report failure.
    return check(repo, warnings_as_errors=False)


def check_tag(
    repo: Path,
    tag: str,
    warnings_as_errors: bool,
    allow_prereleases: bool = False,
) -> int:
    """Gate: run `check`, then verify source_of_truth == TAG (v-prefix stripped).

    The tag compare is the CAP-CI-001 case: a `v0.17.0` tag pointing at a
    commit whose source_of_truth still said 0.16.0. `check` only compares
    locations against each other, so a tag-vs-location drift passes it. This
    mode closes that gap.

    A tag is compared against the source of truth without its `v` prefix:
    `check-tag v2.4.3` against source of truth `2.4.3` is the same version.

    When `allow_prereleases` is False (default), a prerelease tag such as
    `v2.4.3-rc.1` is rejected with a named :class:`PrereleaseNotAllowedError`
    rather than blamed on an incomparable semver string.
    """
    rc = check(repo, warnings_as_errors, allow_prereleases)
    if rc != 0:
        return rc
    decl, _ = load(repo)
    sot = decl["source_of_truth"]
    val = read_version(repo / sot, _pattern_for(decl["locations"], sot))
    if val is None:
        _die(f"source_of_truth {sot!r} unreadable or pattern did not match")

    if tag.startswith("v"):
        normalized_tag = tag[1:]
    else:
        normalized_tag = tag

    if _is_prerelease(normalized_tag) and not allow_prereleases:
        raise PrereleaseNotAllowedError(
            f"tag {tag!r} is a prerelease; pass --allow-prereleases to permit it"
        )

    if val != normalized_tag:
        print(
            f"version-sync: FAIL — tag {normalized_tag!r} != source_of_truth "
            f"{sot} {val!r}"
        )
        return 1
    print(f"version-sync: OK — tag {normalized_tag!r} == {sot} {val!r}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="version-sync")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="gate: verify required locations agree")
    c.add_argument("--warnings-as-errors", action="store_true")
    c.add_argument("--repo", type=Path, default=Path("."))
    ct = sub.add_parser(
        "check-tag", help="gate: check locations AND source_of_truth == TAG"
    )
    ct.add_argument("tag")
    ct.add_argument("--warnings-as-errors", action="store_true")
    ct.add_argument(
        "--allow-prereleases",
        action="store_true",
        help="permit semver prerelease tags/versions (e.g. v2.4.3-rc.1)",
    )
    ct.add_argument("--repo", type=Path, default=Path("."))
    b = sub.add_parser("bump", help="bump: write new version then self-check")
    b.add_argument("new_version")
    b.add_argument("--include-informational", action="store_true")
    b.add_argument("--repo", type=Path, default=Path("."))
    args = p.parse_args()

    if args.cmd == "check":
        return check(args.repo, args.warnings_as_errors)
    if args.cmd == "check-tag":
        return check_tag(
            args.repo, args.tag, args.warnings_as_errors, args.allow_prereleases
        )
    return bump(args.repo, args.new_version, args.include_informational)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PrereleaseNotAllowedError as exc:
        _die(str(exc))
