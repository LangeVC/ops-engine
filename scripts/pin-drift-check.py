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
ADP-010's absent-vocabulary) — plus a third for a present-but-unusable register:

- **No register supplied** is *nothing to check*, not an error: the check
  reports by name that it has nothing to check and exits zero.
- **A register that contradicts a pin** *is* an error: the check fails, names
  the layover's declared pin and the engine's version, and exits non-zero.
- **A present but malformed register** is a named refusal (exit non-zero, no
  traceback): refused as ``pin-drift-check: ERROR: <path>: <reason>``.

Running-version leg (D16). A register entry may carry ``health_url``: the URL
of that layover's ``/health`` endpoint. The check fetches it and reads the
``version`` key of the JSON answer — the ``version`` field of ``RuntimeIdentity``
(``src/ops_engine/runtime_identity.py``, which also carries ``config_sha256``).
These two names are fixed by D16; no others are accepted.

- **running version equals the declared pin** — reported, no drift.
- **running version differs from the declared pin** — drift: exit 1, both
  versions named.
- **``/health`` unreachable, not 2xx, not a JSON object, or without a usable
  ``version``** (absent, empty, ``"unknown"``) — reported as ``INCONCLUSIVE``.
  Inconclusive is never a pass: it exits 3, unless drift is also found (exit 1).
- **an entry without ``health_url``** keeps the pin-only behaviour above.

Exit codes: 0 all clear, 1 drift, 2 refused register, 3 inconclusive.

Stdlib only, no external deps, so it runs unattended on a clean runner.

Usage:
    pin-drift-check.py [--layovers PATH] [--repo PATH]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

PYPROJECT = "pyproject.toml"

HEALTH_URL_KEY = "health_url"  # register field (D16)
RUNNING_VERSION_KEY = "version"  # JSON key of the /health answer (D16)
HEALTH_TIMEOUT_SECONDS = 5
UNKNOWN = "unknown"  # RuntimeIdentity's value for an undeterminable version

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


class LayoverRegisterError(ValueError):
    """A caller-supplied layover register is malformed.

    Raised naming the register's structure (schema, package, entries), so a
    broken register is a refusal that identifies its source — never a raw
    traceback. Mirrors the repo's settled named-refusal discipline for
    externally supplied config (:class:`OpsYamlError`) and
    ``mirror-destination-propose.py``'s ``prog: ERROR: <path>: <reason>``.
    """


def parse_register(text: str) -> list[dict]:
    """Parse a layover register into a list of ``{name, pin}`` entries.

    Raises :class:`LayoverRegisterError` for every malformed shape — JSON that
    does not parse, a top-level list or scalar, a wrong schema or package, an
    empty layovers list, or an entry missing name/pin.
    """
    try:
        decl = json.loads(text)
    except json.JSONDecodeError as e:
        raise LayoverRegisterError(f"invalid JSON: {e}") from e
    if not isinstance(decl, dict):
        raise LayoverRegisterError(
            f"not a schema-1 object (got {type(decl).__name__})"
        )
    if decl.get("schema") != 1:
        raise LayoverRegisterError(f"unsupported schema {decl.get('schema')!r}")
    if decl.get("package") != "ops_engine":
        raise LayoverRegisterError(f"unexpected package {decl.get('package')!r}")
    layovers = decl.get("layovers")
    if not layovers:
        raise LayoverRegisterError("no layovers declared in the register")
    for entry in layovers:
        if not isinstance(entry, dict) or not entry.get("name") or not entry.get("pin"):
            raise LayoverRegisterError(f"layover entry missing name or pin: {entry!r}")
        if HEALTH_URL_KEY in entry and (
            not isinstance(entry[HEALTH_URL_KEY], str) or not entry[HEALTH_URL_KEY]
        ):
            raise LayoverRegisterError(
                f"layover {entry['name']!r}: {HEALTH_URL_KEY} must be a non-empty string"
            )
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


def fetch_running_version(url: str) -> tuple[str | None, str]:
    """Return ``(version, "")`` or ``(None, reason)`` when inconclusive."""
    try:
        with urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT_SECONDS) as resp:
            body = resp.read()
    except Exception as e:  # unreachable, refused, timeout, HTTP error, bad URL
        return None, f"{type(e).__name__}: {e}"
    try:
        answer = json.loads(body)
    except ValueError:
        return None, "answer is not JSON"
    if not isinstance(answer, dict):
        return None, "answer is not a JSON object"
    version = answer.get(RUNNING_VERSION_KEY)
    if not isinstance(version, str) or not version.strip() or version == UNKNOWN:
        return None, f"answer has no usable {RUNNING_VERSION_KEY!r} key"
    return version, ""


def check_running(layovers: list[dict]) -> list[dict]:
    """Probe every layover that declares ``health_url``.

    Returns one result per probed layover:
    ``{name, pin, running, status, detail}`` with status ``match``, ``drift``
    or ``inconclusive``.
    """
    results = []
    for entry in layovers:
        url = entry.get(HEALTH_URL_KEY)
        if not url:
            continue
        running, reason = fetch_running_version(url)
        if running is None:
            status, detail = "inconclusive", reason
        else:
            try:
                same = parse_semver(running) == parse_semver(entry["pin"])
            except ValueError:
                same = running.strip().lstrip("v") == entry["pin"].strip().lstrip("v")
            status, detail = ("match" if same else "drift"), ""
        results.append(
            {
                "name": entry["name"],
                "pin": entry["pin"],
                "running": running,
                "status": status,
                "detail": detail,
            }
        )
    return results


def report_running(results: list[dict]) -> None:
    name_w = max(len(r["name"]) for r in results)
    print()
    print("pin-drift-check: declared pin vs running version (from /health)")
    print()
    print(f"{'layover':<{name_w}}  {'pin':<8} {'running':<8} status")
    for r in sorted(results, key=lambda x: x["name"]):
        running = r["running"] or "-"
        status = r["status"].upper() if r["status"] == "inconclusive" else r["status"]
        print(f"{r['name']:<{name_w}}  {r['pin']:<8} {running:<8} {status}")


def report_running_problems(results: list[dict]) -> None:
    for r in results:
        if r["status"] == "drift":
            print(
                f"{r['name']}: declares {r['pin']}, but is running {r['running']}",
                file=sys.stderr,
            )
        elif r["status"] == "inconclusive":
            print(
                f"{r['name']}: INCONCLUSIVE, running version unknown "
                f"(declares {r['pin']}): {r['detail']}",
                file=sys.stderr,
            )


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
    try:
        layovers = parse_register(args.layovers.read_text(encoding="utf-8"))
    except LayoverRegisterError as e:
        print(
            f"pin-drift-check: ERROR: {args.layovers}: {e}",
            file=sys.stderr,
        )
        return 2
    drift = drift_for(layovers, latest)
    running = check_running(layovers)
    running_drift = any(r["status"] == "drift" for r in running)
    inconclusive = any(r["status"] == "inconclusive" for r in running)

    if drift:
        report_drift(drift, latest)
    else:
        report_green(layovers, latest)
    if running:
        report_running(running)
        report_running_problems(running)

    if drift or running_drift:
        return 1
    if inconclusive:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
