"""REA-015: one entry point for repository workflows — attach and verify.

A repository workflow builds its declared files and then runs one command:

    ops-engine-release-attach --config config.yml --repo org/name --tag v1.2.3 \
        --artifacts dist --repo-dir . --notes-file NOTES.md

For every declared release destination it finds or creates the release,
converges it to the intended state (REA-012), attaches the files (REA-013) and
verifies the declared asset set by reading the release back (REA-014). The
same argv works from a Forgejo runner and a GitHub runner: the runner supplies
the credentials its workflow already holds, and the command resolves each
destination from the same config the engine uses.

The command is a thin wire around :meth:`ReleaseHandler.publish_release`, not
a second publication path: it loads the layers the engine loads, passes
``repo_dir`` and ``audit`` so the declared-asset verification fires, and turns
the returned :class:`PublicationReport` into the machine-readable report
(schema v1) documented in CONTRACT.md.

Exit codes (CONTRACT.md, "Release attach CLI"):

* ``0`` every destination is done: published, converged, already equal or
  verified.
* ``1`` at least one destination failed by name; the report's ``failure``
  strings name the file and the destination.
* ``2`` the command could not resolve what to publish (a bad config, a
  missing artifact directory, an unknown forge): a named usage error, printed
  as the same report shape with ``ok: false``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from ops_engine.adapters.base import ForgeAdapter
from ops_engine.adapters.factory import Credential
from ops_engine.config_loader import OpsEngineConfig, load_config_file
from ops_engine.modules.release import (
    DEFAULT_NAME_TEMPLATE,
    PublicationReport,
    ReleaseHandler,
)

SCHEMA = "ops-engine-release-attach-report/v1"

# Exit codes, documented in CONTRACT.md.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


class AttachUsageError(ValueError):
    """The command cannot resolve what to publish; the message names the input."""


@dataclass
class AttachRequest:
    """What one attach run is asked to do, resolved from the command line."""

    config: OpsEngineConfig
    repo: str
    tag_name: str
    artifact_dir: Path
    repo_dir: Path
    name_template: str
    notes: str
    release_body: Optional[str]
    # None means "not supplied": the engine then falls back to the repository's
    # declared tracker_prefixes (REA-018). An empty list would instead say
    # "supplied, and it names nothing".
    ticket_prefixes: Optional[list[str]]
    credentials: dict[str, Credential]


def request_from_args(
    args: argparse.Namespace, environment: Optional[dict[str, str]] = None
) -> AttachRequest:
    """Resolve the command line into a request, refusing a named input error.

    The credentials come from the environment variables each destination
    declares (``credential_variable``) — the same variable the shipped
    workflow populates — so the same argv is correct on either runner. A
    variable that names a secret is read by that name; ``environment`` is the
    runner's environment (``os.environ`` by default).

    Raises:
        AttachUsageError: the config file cannot be read or parsed, or the
            artifact directory is not a directory.
    """
    env = os.environ if environment is None else environment
    try:
        config = load_config_file(args.config)
    except Exception as exc:  # noqa: BLE001 (a usage error, reported by name)
        raise AttachUsageError(f"cannot load config {args.config}: {exc}") from exc

    artifact_dir = Path(args.artifacts)
    if not artifact_dir.is_dir():
        raise AttachUsageError(
            f"artifact directory {artifact_dir} does not exist or is not a directory"
        )

    credentials: dict[str, Credential] = {}
    # The destinations already resolved from the config say which variables
    # supply their credentials; the command reads exactly those.
    for destination in _all_destinations(config):
        variable = destination.credential_variable
        if not variable or variable in credentials:
            continue
        token = env.get(variable, "")
        if token:
            credentials[variable] = Credential(token=token)

    notes = ""
    release_body = None
    try:
        if args.notes_file:
            notes = Path(args.notes_file).read_text(encoding="utf-8")
        if args.body_file:
            release_body = Path(args.body_file).read_text(encoding="utf-8")
    except OSError as exc:
        raise AttachUsageError(f"cannot read a text input: {exc}") from exc

    return AttachRequest(
        config=config,
        repo=args.repo,
        tag_name=args.tag,
        artifact_dir=artifact_dir,
        repo_dir=Path(args.repo_dir or "."),
        name_template=args.name_template or DEFAULT_NAME_TEMPLATE,
        notes=notes,
        release_body=release_body,
        ticket_prefixes=list(args.ticket_prefix) if args.ticket_prefix else None,
        credentials=credentials,
    )


def _all_destinations(config: OpsEngineConfig) -> Iterable:
    """Yield every declared destination across the config's orgs.

    The command needs the credential variable names before it knows which
    repository it serves; reading them all is a lookup, not a publication.
    """
    for org in config.orgs.values():
        for repo in org.repositories.values():
            yield from repo.destinations


async def run_attach(
    config: OpsEngineConfig,
    repo: str,
    tag_name: str,
    artifact_dir: str | Path,
    *,
    adapters: Optional[list[ForgeAdapter]] = None,
    repo_dir: str | Path,
    name_template: str = DEFAULT_NAME_TEMPLATE,
    notes: str = "",
    release_body: Optional[str] = None,
    ticket_prefixes: Optional[Iterable[str]] = None,
    credentials: Optional[dict[str, Credential]] = None,
    audit: Optional[list[str]] = None,
    skip_existing: bool = True,
    asset_downloader: object = None,
    free_space_reader: object = None,
) -> PublicationReport:
    """Run one attach and return the engine's publication report.

    ``repo_dir`` and ``audit`` are always passed: ``repo_dir`` lets the engine
    merge the Layer-3 ``.ops.yaml`` (so the declared assets are read and
    verified), and ``audit`` receives one line per destination. ``adapters`` is
    the test injection point of :meth:`ReleaseHandler.publish_release` and
    never changes the destination resolution. ``asset_downloader`` and
    ``free_space_reader`` are the REA-017 injection points; ``None`` lets the
    engine build the shipped HTTP collaborators.
    """
    return await ReleaseHandler.publish_release(
        config,
        repo,
        tag_name,
        notes,
        artifact_dir,
        name_template=name_template,
        adapters=adapters,
        repo_dir=repo_dir,
        skip_existing=skip_existing,
        audit=audit,
        credentials=credentials,
        release_body=release_body,
        ticket_prefixes=ticket_prefixes,
        asset_downloader=asset_downloader,
        free_space_reader=free_space_reader,
    )


def _outcome(report: PublicationReport, label: str) -> str:
    """The single outcome of a destination, in precedence order."""
    if label in report.failed:
        return "failed"
    if label in report.published:
        return "published"
    if label in report.supplemented:
        return "converged"
    if label in report.skipped:
        return "skipped"
    if label in report.up_to_date:
        return "up-to-date"
    if label in report.rc_skipped:
        return "rc-skipped"
    return "verified" if label in report.verified else "unknown"


def _labels(report: PublicationReport) -> list[str]:
    """Every destination the run touched, in the order it first appears.

    ``pulled``, ``free_space`` and ``pending`` are read after the outcome
    buckets because a reading is recorded even when nothing else about the
    destination changed (REA-017): a destination that only carries evidence
    still belongs in the report.
    """
    seen: list[str] = []
    for bucket in (
        report.published,
        report.supplemented,
        report.skipped,
        report.up_to_date,
        report.rc_skipped,
        report.failed,
        list(report.verified),
        list(report.pulled),
        list(report.pending),
        list(report.free_space),
    ):
        for label in bucket:
            if label not in seen:
                seen.append(label)
    return seen


def _pulled_names(entries: Iterable[str]) -> list[str]:
    """The file names out of the engine's ``<name> sha256:<hex>`` entries.

    The report names the files, not their digests: the digest is the same one
    the verified list already carries, and a consumer that wants to compare
    pins it there.
    """
    return [entry.split(" ", 1)[0] for entry in entries]


def _pending_entries(entries: Iterable[str]) -> list[dict]:
    """Parse the engine's ``<name> producer=<forge> since=<iso>`` text.

    A piece still on its way is reported as the structured fact it is —
    which file, from which forge, since when — so a consumer does not have to
    parse a sentence to see that the publish is not yet complete.
    """
    parsed = []
    for entry in entries:
        parts = entry.split(" ")
        name, producer, since = parts[0], "", ""
        for part in parts[1:]:
            if part.startswith("producer="):
                producer = part.split("=", 1)[1]
            elif part.startswith("since="):
                since = part.split("=", 1)[1]
        parsed.append({"name": name, "producer": producer, "since": since})
    return parsed


def _free_space(reading: Optional[str]):
    """The free-space reading as the report shows it: a byte count or a reason.

    A ``free_bytes=<n>`` reading becomes the integer; anything else is the
    engine's ``unavailable: <reason>`` string, passed through verbatim. It is
    never ``null``: a missing reading is evidence about the host, not a hole.
    """
    if reading is None:
        return "unavailable: no reading recorded"
    if reading.startswith("free_bytes="):
        try:
            return int(reading.split("=", 1)[1])
        except ValueError:
            return reading
    return reading


def render_report(report: PublicationReport) -> dict:
    """Serialize a publication report to the machine-readable schema v1.

    One entry per destination, in the engine's order: the destination label,
    its single outcome, the verified ``<name> sha256:<hex>`` list (empty when
    nothing was declared) and the failure detail naming the file and the
    destination (``None`` on success). REA-017 adds, per destination, the
    pulled file names, the host free-space reading (an integer, or an explicit
    ``unavailable: <reason>`` string — never null) and the still-pending
    pieces with their producer and since. ``exit_code`` is the code the
    command returns, so a consumer reads the outcome from the report alone.
    """
    destinations = []
    for label in _labels(report):
        detail = report.failure_details.get(label)
        # The failure names both the destination and what differs, so a
        # consumer that reads only the string still knows where it failed.
        failure = f"{label}: {detail}" if detail else None
        destinations.append(
            {
                "destination": label,
                "outcome": _outcome(report, label),
                "verified": list(report.verified.get(label, [])),
                "failure": failure,
                "pulled": _pulled_names(report.pulled.get(label, [])),
                "free_space": _free_space(report.free_space.get(label)),
                "pending": _pending_entries(report.pending.get(label, [])),
            }
        )
    failed = bool(report.failed)
    return {
        "schema": SCHEMA,
        "repo": report.repo,
        "tag_name": report.tag_name,
        "ok": not failed,
        "exit_code": EXIT_FAILED if failed else EXIT_OK,
        "destinations": destinations,
    }


def usage_report(message: str) -> dict:
    """The report shape for a run that could not start, with no destinations."""
    return {
        "schema": SCHEMA,
        "repo": "",
        "tag_name": "",
        "ok": False,
        "exit_code": EXIT_USAGE,
        "error": message,
        "destinations": [],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ops-engine-release-attach",
        description=(
            "Find or create a release, converge it, attach the built files "
            "and verify the declared asset set on every destination."
        ),
    )
    parser.add_argument("--config", required=True, help="the Layer-2 org config file")
    parser.add_argument("--repo", required=True, help="the repository selector, org/name")
    parser.add_argument("--tag", required=True, help="the tag being released")
    parser.add_argument(
        "--artifacts", required=True, help="the directory of built files to attach"
    )
    parser.add_argument(
        "--repo-dir",
        default=".",
        help="the repository root holding .ops.yaml (default: the current directory)",
    )
    parser.add_argument("--name-template", default="", help="the release name template")
    parser.add_argument(
        "--notes-file", default="", help="a file holding the tag's release notes"
    )
    parser.add_argument(
        "--body-file", default="", help="a file holding the composed release body"
    )
    parser.add_argument(
        "--ticket-prefix",
        action="append",
        default=None,
        help=(
            "an external tracker prefix the body must not name (repeatable); "
            "absent, the repository's declared tracker_prefixes arm the gate"
        ),
    )
    return parser


def main(
    argv: Optional[list[str]] = None,
    *,
    adapters: Optional[list[ForgeAdapter]] = None,
    asset_downloader: object = None,
    free_space_reader: object = None,
) -> int:
    """Run the command, print the JSON report and return the exit code.

    ``argv`` is the command line (``sys.argv[1:]`` by default); ``adapters``,
    ``asset_downloader`` and ``free_space_reader`` are the test injection
    points passed through to :func:`run_attach`. They do not change what the
    command resolves or publishes.
    """
    args = _parser().parse_args(argv)
    try:
        request = request_from_args(args)
    except AttachUsageError as exc:
        print(json.dumps(usage_report(str(exc)), indent=2))
        return EXIT_USAGE

    audit: list[str] = []
    try:
        report = asyncio.run(
            run_attach(
                request.config,
                request.repo,
                request.tag_name,
                request.artifact_dir,
                adapters=adapters,
                repo_dir=request.repo_dir,
                name_template=request.name_template,
                notes=request.notes,
                release_body=request.release_body,
                ticket_prefixes=request.ticket_prefixes,
                credentials=request.credentials,
                audit=audit,
                asset_downloader=asset_downloader,
                free_space_reader=free_space_reader,
            )
        )
    except Exception as exc:  # noqa: BLE001 (a named failure, never a traceback)
        print(json.dumps(usage_report(f"{type(exc).__name__}: {exc}"), indent=2))
        return EXIT_USAGE

    payload = render_report(report)
    print(json.dumps(payload, indent=2))
    return payload["exit_code"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
