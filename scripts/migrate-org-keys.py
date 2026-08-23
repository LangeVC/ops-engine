#!/usr/bin/env python3
"""migrate-org-keys — migrate display-case org keys to canonical lowercase keys.

FFR-200-2. Forgejo canonicalises every org name to a lowercase ``lower_name``.
Layover ``config.yml`` files predating that convention keyed ``orgs`` on the
display-cased name (``fusionAIze``) or a GitHub handle (``LangeVC``). This tool
rekeys every org onto ``key.lower()`` and preserves the old key as
``github.login`` so the display-case identity is not lost.

Two org keys that lower to the same canonical key (e.g. ``fusionAIze`` and
``FusionAIze``) cannot both survive the migration — that is a silent-collision
hazard, so the load fails with a named ``OrgKeyCollisionError`` naming both
keys instead of letting one silently win.

Stdlib only, no external deps.

Usage:
    migrate-org-keys.py migrate FILE [--in-place]
    migrate-org-keys.py check FILE

``migrate`` prints the migrated YAML to stdout (or rewrites FILE with
``--in-place``). ``check`` verifies the file is already canonical and exits
non-zero on any display-case key or collision.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, NoReturn


class OrgKeyCollisionError(Exception):
    """Two org keys lower to the same canonical key.

    Raised instead of silently letting one key win, so a config that carries
    both a display-case and a lowercased spelling of the same org is surfaced
    before any lookup silently drops one.
    """

    def __init__(self, canonical: str, keys: tuple[str, ...]) -> None:
        self.canonical = canonical
        self.keys = keys
        quoted = ", ".join(repr(k) for k in keys)
        super().__init__(
            f"org key collision after lowercasing to {canonical!r}: {quoted}"
        )


def _yaml_load(text: str) -> Any:
    """Load YAML, trying the optional ``yaml`` package first.

    The ops-engine package depends on PyYAML, so a layover running this tool
    alongside it has it available. If it is missing we refuse rather than
    hand-rolling a YAML parser that would silently mis-parse real config.
    """
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - exercised only without pyyaml
        raise RuntimeError(
            "PyYAML is required to parse config; install with: pip install pyyaml"
        ) from None
    return yaml.safe_load(text)


def _yaml_dump(data: Any) -> str:
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover
        raise RuntimeError(
            "PyYAML is required to write config; install with: pip install pyyaml"
        ) from None
    # allow_unicode so non-ASCII display names round-trip unescaped.
    return yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False, default_flow_style=False
    )


def migrate_orgs(orgs: dict[str, Any]) -> dict[str, Any]:
    """Rekey ``orgs`` onto lowercase canonical keys, preserving old keys as
    ``github.login``.

    Returns a new mapping; the input is not mutated. Every org key is lowercased
    to its canonical form, and the original key is written into the org's
    ``github.login`` attribute unless an explicit ``github.login`` already
    exists. Two keys that lower to the same canonical string raise
    :class:`OrgKeyCollisionError` naming both.
    """
    migrated: dict[str, Any] = {}
    canonical_to_source: dict[str, str] = {}
    for key, value in orgs.items():
        canonical = key.lower()
        previous = canonical_to_source.get(canonical)
        if previous is not None:
            raise OrgKeyCollisionError(canonical, (previous, key))
        canonical_to_source[canonical] = key

        if not isinstance(value, dict):
            # A non-mapping org value is left untouched; the migration only
            # concerns the key. It rekeys without inspecting the body.
            migrated[canonical] = value
            continue

        org = dict(value)
        github = org.get("github")
        if not isinstance(github, dict):
            github = {}
        if "login" not in github:
            github["login"] = key
        org["github"] = github
        migrated[canonical] = org

    return migrated


def migrate_config(config: dict[str, Any]) -> dict[str, Any]:
    """Migrate a full config mapping's ``orgs`` section and return a new mapping."""
    if not isinstance(config, dict):
        raise ValueError("config must be a mapping")
    result = dict(config)
    orgs = config.get("orgs")
    if orgs is None:
        orgs = {}
    if not isinstance(orgs, dict):
        raise ValueError("config['orgs'] must be a mapping")
    result["orgs"] = migrate_orgs(orgs)
    return result


def _die(msg: str) -> NoReturn:
    print(f"migrate-org-keys: ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def _read_config(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        _die(f"{path} does not exist")
    text = path.read_text(encoding="utf-8")
    data = _yaml_load(text)
    if not isinstance(data, dict):
        _die(f"{path} did not parse as a mapping")
    return data, text


def _run_migrate(path: Path, in_place: bool) -> int:
    data, _ = _read_config(path)
    try:
        migrated = migrate_config(data)
    except OrgKeyCollisionError as exc:
        print(f"migrate-org-keys: collision: {exc}", file=sys.stderr)
        return 1
    out = _yaml_dump(migrated)
    if in_place:
        path.write_text(out, encoding="utf-8")
        print(f"migrated {path} in place")
    else:
        sys.stdout.write(out)
    return 0


def _run_check(path: Path) -> int:
    data, _ = _read_config(path)
    orgs = data.get("orgs")
    if orgs is None:
        orgs = {}
    if not isinstance(orgs, dict):
        _die("config['orgs'] must be a mapping")

    failed = False
    try:
        migrated = migrate_orgs(orgs)
    except OrgKeyCollisionError as exc:
        print(f"migrate-org-keys: collision: {exc}", file=sys.stderr)
        return 1

    for key in orgs:
        if key != key.lower():
            print(f"migrate-org-keys: display-case key {key!r}", file=sys.stderr)
            failed = True
    if failed:
        print("migrate-org-keys: FAIL — display-case org keys present")
        return 1
    print(f"migrate-org-keys: OK — {len(migrated)} canonical org key(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="migrate-org-keys")
    sub = parser.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("migrate", help="print migrated YAML (or rewrite with --in-place)")
    m.add_argument("file", type=Path)
    m.add_argument("--in-place", action="store_true")
    c = sub.add_parser("check", help="verify keys are already canonical")
    c.add_argument("file", type=Path)
    args = parser.parse_args(argv)

    if args.cmd == "migrate":
        return _run_migrate(args.file, args.in_place)
    return _run_check(args.file)


if __name__ == "__main__":
    raise SystemExit(main())
