#!/usr/bin/env python3
"""pin-drift-check — report per-layover pin drift against the current contract.

A layover's pin (``@v2.0.0``) says which ops-engine release it runs. The
consumption declaration (``docs/layover-consumption.md``) says which contract
names it consumes. A consumed name that was introduced *after* the layover's
pin is drift: the layover relies on a name its pinned release does not provide.

This check reads the consumption declaration and the public-surface contract
(``CONTRACT.md``), reconstructs when each contract name was introduced from the
git release tags, and reports, per layover, its pin, the latest version, and the
consumed names that changed after that pin.

Stdlib only, no external deps, so it runs unattended on a clean runner.

Usage:
    pin-drift-check.py [--repo PATH]
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

LAYOVER_DOC = "docs/layover-consumption.md"
CONTRACT_DOC = "CONTRACT.md"
PYPROJECT = "pyproject.toml"
INIT_PATH = "src/ops_engine/__init__.py"

VERSION_RE = re.compile(r'^version\s*=\s*"(\d+\.\d+\.\d+)"')
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _die(msg: str) -> "NoReturn":
    print(f"pin-drift-check: ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def parse_semver(version: str) -> tuple[int, ...]:
    """Return the numeric parts of a version, tolerating a leading ``v``."""
    v = version.strip().lstrip("v")
    if not v or not all(part.isdigit() for part in v.split(".")):
        raise ValueError(f"not a semver: {version!r}")
    return tuple(int(part) for part in v.split("."))


def version_gt(a: str, b: str) -> bool:
    return parse_semver(a) > parse_semver(b)


def extract_json_fence(text: str) -> dict:
    m = re.search(r"```json\n(.*?)\n```", text, re.DOTALL)
    if not m:
        raise ValueError("no ```json``` declaration block found")
    return json.loads(m.group(1))


def parse_layovers(text: str) -> list[dict]:
    """Parse the layover consumption declaration into a list of layovers."""
    decl = extract_json_fence(text)
    if decl.get("schema") != 1:
        raise ValueError(f"unsupported schema {decl.get('schema')!r}")
    if decl.get("package") != "ops_engine":
        raise ValueError(f"unexpected package {decl.get('package')!r}")
    layovers = decl.get("layovers")
    if not layovers:
        raise ValueError("no layovers declared")
    return layovers


def parse_contract_names(text: str) -> list[str]:
    """Return the contract export names declared in CONTRACT.md."""
    decl = extract_json_fence(text)
    return [entry["name"] for entry in decl["exports"]]


def parse_all_from_source(text: str) -> set[str]:
    """Extract the ``__all__`` names from ``src/ops_engine/__init__.py`` source."""
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            return set(ast.literal_eval(node.value))
    raise ValueError("no __all__ assignment found")


def read_latest_version(repo: Path) -> str:
    text = (repo / PYPROJECT).read_text(encoding="utf-8")
    for line in text.splitlines():
        m = VERSION_RE.search(line)
        if m:
            return m.group(1)
    raise ValueError(f"no version found in {PYPROJECT}")


def git_tags(repo: Path) -> list[str]:
    """Return semver release tags, oldest first."""
    r = subprocess.run(
        ["git", "tag", "--list"], cwd=repo, capture_output=True, text=True
    )
    if r.returncode != 0:
        raise RuntimeError(f"git tag failed: {r.stderr.strip()}")
    tags = []
    for raw in r.stdout.splitlines():
        tag = raw.strip()
        v = tag.lstrip("v")
        if v and SEMVER_RE.match(v):
            tags.append(tag)
    return sorted(tags, key=parse_semver)


def git_init_at(repo: Path, tag: str) -> str:
    r = subprocess.run(
        ["git", "show", f"{tag}:{INIT_PATH}"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"git show {tag}:{INIT_PATH} failed: {r.stderr.strip()}")
    return r.stdout


def build_timeline_from_tagged_names(
    tagged_names: dict[str, set[str]], current_names: set[str], latest: str
) -> dict[str, str]:
    """Map each contract name to the version that first introduced it.

    ``tagged_names`` maps an ascending-ordered release tag to the ``__all__``
    names present at that tag. A name still unknown after the last tag is
    treated as introduced by the current (latest) version.
    """
    timeline: dict[str, str] = {}
    for tag, names in tagged_names.items():
        for name in names:
            if name not in timeline:
                timeline[name] = tag.lstrip("v")
    for name in current_names:
        if name not in timeline:
            timeline[name] = latest
    return timeline


def build_timeline(repo: Path, current_names: set[str], latest: str) -> dict[str, str]:
    tags = git_tags(repo)
    if not tags:
        raise RuntimeError(
            "no semver release tags found; cannot reconstruct the contract timeline"
        )
    tagged_names = {tag: parse_all_from_source(git_init_at(repo, tag)) for tag in tags}
    return build_timeline_from_tagged_names(tagged_names, current_names, latest)


def changed_consumed_names(
    layover: dict, timeline: dict[str, str], latest: str
) -> list[str]:
    """Return the consumed names introduced after the layover's pin, sorted."""
    pin = layover["pin"]
    changed = []
    for name in layover.get("consumes", []):
        introduced = timeline.get(name, latest)
        if version_gt(introduced, pin):
            changed.append(name)
    return sorted(changed)


def report(layovers: list[dict], timeline: dict[str, str], latest: str) -> None:
    name_w = max(len(l["name"]) for l in layovers)
    print("pin-drift-check: per-layover pin vs latest contract drift")
    print()
    print(f"{'layover':<{name_w}}  {'pin':<8} {'latest':<8} changed-consumed-names")
    print(f"{'-' * name_w}  {'---':<8} {'------':<8} ---------------------")
    for l in sorted(layovers, key=lambda x: x["name"]):
        changed = changed_consumed_names(l, timeline, latest)
        names = ",".join(changed) if changed else "-"
        print(f"{l['name']:<{name_w}}  {l['pin']:<8} {latest:<8} {names}")


def main() -> int:
    p = argparse.ArgumentParser(prog="pin-drift-check")
    p.add_argument("--repo", type=Path, default=Path("."))
    args = p.parse_args()
    repo = args.repo

    layover_doc = repo / LAYOVER_DOC
    if not layover_doc.exists():
        _die(f"missing {layover_doc}")
    layovers = parse_layovers(layover_doc.read_text(encoding="utf-8"))

    contract_doc = repo / CONTRACT_DOC
    if not contract_doc.exists():
        _die(f"missing {contract_doc}")
    current_names = set(
        parse_contract_names(contract_doc.read_text(encoding="utf-8"))
    )

    latest = read_latest_version(repo)
    timeline = build_timeline(repo, current_names, latest)

    report(layovers, timeline, latest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
