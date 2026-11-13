#!/usr/bin/env python3
"""release-body-drift — report release body drift on a destination (P36-018).

Read-only check: looks up a release for a tag on one destination and reports
whether the body is empty or lacks the CHANGELOG section. Exits non-zero when
drift is found, zero when the body is correct or there is no release. The
check never writes — the report is for a cockpit or monitoring check.

Exit codes: 0 no drift (body correct or no release), 1 drift found, 2 usage error.

Usage:
    release-body-drift.py --repo OWNER/NAME --tag vX.Y.Z \\
        --forge {forgejo|github} [--base-url URL] [--token TOKEN] \\
        [--release-notes NOTES]
"""

from __future__ import annotations

import argparse
import asyncio
import sys


async def _check(adapter, repo: str, tag_name: str, release_notes: str) -> int:
    from ops_engine.modules.reconcile import ReconcileHandler

    result = await ReconcileHandler.check_release_body(
        adapter, repo, tag_name, release_notes=release_notes,
    )
    if result is None:
        print(f"release-body-drift: {repo} {tag_name}: no drift")
        return 0
    if result.empty:
        print(
            f"release-body-drift: DRIFT {repo} {tag_name}: "
            f"body is empty (release_id={result.release_id})",
            file=sys.stderr,
        )
        return 1
    if result.missing_changelog:
        print(
            f"release-body-drift: DRIFT {repo} {tag_name}: "
            f"body lacks CHANGELOG section (release_id={result.release_id})",
            file=sys.stderr,
        )
        return 1
    return 0


def _build_adapter(args) -> tuple:
    from ops_engine.adapters.factory import Credential, adapters_for
    from ops_engine.config_loader import Destination

    token = args.token or ""
    dest = Destination(forge=args.forge, repo=args.repo, role="release")
    cred = Credential(token=token, webhook_secret="")
    adapters = adapters_for(
        [dest],
        credentials={args.forge: cred},
        base_url=args.base_url or "",
    )
    return adapters[0]


async def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="release-body-drift")
    p.add_argument("--repo", required=True, help="OWNER/NAME of the destination repo")
    p.add_argument("--tag", required=True, help="tag name, e.g. v1.2.3")
    p.add_argument(
        "--forge", required=True, choices=["forgejo", "github"],
        help="destination forge",
    )
    p.add_argument("--base-url", default="", help="Forgejo instance base URL")
    p.add_argument("--token", default="", help="API token for the destination")
    p.add_argument(
        "--release-notes", default="",
        help="CHANGELOG section text to compare against",
    )
    args = p.parse_args(argv)

    adapter = _build_adapter(args)
    return await _check(adapter, args.repo, args.tag, args.release_notes)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
