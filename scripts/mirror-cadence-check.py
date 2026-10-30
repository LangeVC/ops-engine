#!/usr/bin/env python3
"""mirror-cadence-check — report a mirrored repository whose cadence deviates.

The cadence rule is stated once in ``docs/mirror-cadence.md``: a mirrored
repository follows the default drift window unless it carries a *named
exception*. This check reads the machine-readable declaration in that document
and reports every mirrored repository whose drift window deviates from the
default and is not covered by a named exception.

Mechanics live here (in ``ops-engine``); policy lives in the layover. This
check states a deviation so a human can either correct the value or record the
exception — it never decides for them, and it never rewrites the document.

Stdlib only, no external deps, so it runs unattended on a clean runner.

Exit status:
    0 — every mirror follows the default or is covered by a named exception.
    1 — at least one mirror deviates and is not covered by a named exception.
    2 — the declaration is missing or malformed (a configuration error, not a
        cadence finding).

Usage:
    mirror-cadence-check.py [--doc PATH]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import NoReturn

DEFAULT_DOC = "docs/mirror-cadence.md"


def _die(msg: str) -> NoReturn:
    print(f"mirror-cadence-check: ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def extract_json_fence(text: str) -> dict:
    m = re.search(r"```json\n(.*?)\n```", text, re.DOTALL)
    if not m:
        _die("no ```json``` declaration block found in the cadence document")
    return json.loads(m.group(1))


def parse_declaration(text: str) -> tuple[int, list[dict]]:
    decl = extract_json_fence(text)
    if decl.get("schema") != 1:
        _die(f"unsupported declaration schema {decl.get('schema')!r}")
    default = decl.get("default_drift_seconds")
    if not isinstance(default, int) or default <= 0:
        _die("declaration must declare a positive default_drift_seconds")
    mirrors = decl.get("mirrors")
    if not isinstance(mirrors, list):
        _die("declaration must contain a 'mirrors' list")
    return default, mirrors


def is_excepted(mirror: dict) -> tuple[bool, str | None]:
    exception = mirror.get("exception")
    if not isinstance(exception, dict):
        return False, None
    reason = exception.get("reason")
    return bool(reason), (reason if reason else None)


def deviations(default: int, mirrors: list[dict]) -> list[dict]:
    out = []
    for mirror in mirrors:
        drifv = mirror.get("drift_seconds")
        if drifv == default:
            continue
        excepted, _ = is_excepted(mirror)
        if excepted:
            continue
        out.append(mirror)
    return out


def describe(mirror: dict, default: int) -> str:
    org = mirror.get("org", "<missing-org>")
    repo = mirror.get("repo", "<missing-repo>")
    drifv = mirror.get("drift_seconds", "<missing>")
    return f"{org}/{repo} drift_seconds={drifv} (default {default})"


def main() -> int:
    p = argparse.ArgumentParser(prog="mirror-cadence-check")
    p.add_argument("--doc", type=Path, default=Path(DEFAULT_DOC))
    args = p.parse_args()

    doc = args.doc
    if not doc.exists():
        _die(f"missing cadence document {doc}")

    default, mirrors = parse_declaration(doc.read_text(encoding="utf-8"))
    findings = deviations(default, mirrors)

    if findings:
        print("mirror-cadence-check: cadence deviations without a named exception")
        for m in findings:
            print(f"  {describe(m, default)}")
        return 1

    n = len(mirrors)
    print(
        f"mirror-cadence-check: {n} mirror(s) follow the default cadence "
        f"(drift {default}s) or are covered by a named exception"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
