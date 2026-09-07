#!/usr/bin/env python3
"""pin-drift-check — report per-layover pin drift against the engine's release.

A layover's pin (``@v3.4.0``) says which ops-engine release it runs. The
layover register lists each layover's name and the version it pins. The drift
check reads that register from a path supplied on the command line and compares
each declared pin against the engine's own current version (``pyproject.toml``).
A layover whose declared pin disagrees with the engine's version is drift: the
register lags the release it should describe.

The register is the calling organisation's data, not the template's: it lives
in that organisation's own repository and arrives here as a ``--layovers`` path.
No register is shipped in this template, and no organisation or layover name is
hard-coded in this script.

Two shapes, deliberately not collapsed (ADP-008's missing-destination and
ADP-010's absent-vocabulary):

- **No register supplied** is *nothing to check*, not an error: the check
  reports by name that it has nothing to check and exits zero.
- **A register that contradicts a pin** *is* an error: the check fails, names
  the layover's declared pin and the engine's version, and exits non-zero.

Stdlib only, no external deps, so it runs unattended on a clean runner.

Usage:
    pin-drift-check.py [--layovers PATH] [--repo PATH]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PYPROJECT = "pyproject.toml"

VERSION_RE = re.compile(r'^version\s*=\s*"(\d+\.\d+\.\d+)"')


def parse_semver(version: str) -> tuple[int, ...]:
    """Return the numeric parts of a version, tolerating a leading ``v``."""
    v = version.strip().lstrip("v")
    if not v or not all(part.isdigit() for part in v.split(".")):
        raise ValueError(f"not a semver: {version!r}")
    return tuple(int(part) for part in v.split("."))


def read_latest_version(repo: Path) -> str:
    text = (repo / PYPROJECT).read_text(encoding="utf-8")
    for line in text.splitlines():
        m = VERSION_RE.search(line)
        if m:
            return m.group(1)
    raise ValueError(f"no version found in {PYPROJECT}")


def parse_register(text: str) -> list[dict]:
    """Parse a layover register into a list of ``{name, pin}`` entries."""
    decl = json.loads(text)
    if decl.get("schema") != 1:
        raise ValueError(f"unsupported schema {decl.get('schema')!r}")
    if decl.get("package") != "ops_engine":
        raise ValueError(f"unexpected package {decl.get('package')!r}")
    layovers = decl.get("layovers")
    if not layovers:
        raise ValueError("no layovers declared in the register")
    for entry in layovers:
        if not entry.get("name") or not entry.get("pin"):
            raise ValueError(f"layover entry missing name or pin: {entry!r}")
    return layovers


def drift_for(layovers: list[dict], latest: str) -> list[tuple[str, str]]:
    """Return ``(name, declared_pin)`` for every layover whose declared pin is
    not the engine's current version."""
    latest_parts = parse_semver(latest)
    drifted = []
    for entry in layovers:
        declared_parts = parse_semver(entry["pin"])
        if declared_parts != latest_parts:
            drifted.append((entry["name"], entry["pin"]))
    return drifted


def report_nothing_to_check() -> None:
    print(
        "pin-drift-check: no layover register supplied; nothing to check. "
        "Pass --layovers PATH to check a register against the engine version."
    )


def report_green(layovers: list[dict], latest: str) -> None:
    name_w = max(len(l["name"]) for l in layovers)
    print("pin-drift-check: per-layover pin vs engine version")
    print()
    print(f"{'layover':<{name_w}}  {'pin':<8} {'engine':<8}")
    print(f"{'-' * name_w}  {'---':<8} {'------':<8}")
    for l in sorted(layovers, key=lambda x: x["name"]):
        print(f"{l['name']:<{name_w}}  {l['pin']:<8} {latest:<8}")


def report_drift(drift: list[tuple[str, str]], latest: str) -> None:
    print(
        "pin-drift-check: layover pins disagree with the engine version",
        file=sys.stderr,
    )
    for name, pin in drift:
        print(
            f"{name}: declares {pin}, but the engine is {latest}",
            file=sys.stderr,
        )


def main() -> int:
    p = argparse.ArgumentParser(prog="pin-drift-check")
    p.add_argument("--layovers", type=Path, default=None)
    p.add_argument("--repo", type=Path, default=Path("."))
    args = p.parse_args()
    repo = args.repo

    if args.layovers is None:
        report_nothing_to_check()
        return 0

    latest = read_latest_version(repo)
    layovers = parse_register(args.layovers.read_text(encoding="utf-8"))
    drift = drift_for(layovers, latest)

    if drift:
        report_drift(drift, latest)
        return 1

    report_green(layovers, latest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
