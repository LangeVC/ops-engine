"""P36-004: a readiness report of refused commits per repository.

One command prints, per repository, the refused commits with path and blob SHA
and the count refused over all reachable commits, read-only. The output can be
pasted as exception entries of P36-001 without editing.

Usage::

    python -m ops_engine.modules.visibility_report REPO_DIR [REPO_DIR ...] \
        --glob '.skillweave/**' --glob '**/prd*.json'

For each repository the command prints one section::

    # repo: /path/to/repo
    # commits_refused=2
    # commit 8f8a1cb...
    # commit 2d0f44e...
            visibility_exceptions:
              - path: '.skillweave/prd.json'
                blob: '0967ef424bce6791893e9a57bb952f80fd536e93'

The ``visibility_exceptions:`` block is printed with the indentation a Layover
``config.yml`` uses for a repository key (``--indent``, 8 by default, so the
list dash sits at ``indent + 2``). It names each distinct ``(path, blob)`` pair
once, so pasting the block unchanged under a repository's keys adds exactly
those exceptions; the ``#`` lines are YAML comments and paste with it.

The count is the number of distinct commits whose diff against any parent adds
or removes a candidate path, over the whole history reachable from every ref
(``git log --all``), not just ``HEAD``. It is therefore the number of commits
the visibility gate of P36-001 would refuse without an exception.

Read-only: the report runs ``git log`` and never writes to the repository.
"""

import argparse
import fnmatch
import os
import subprocess
import sys
from collections.abc import Sequence

_CANDIDATE_OBJECT_ID_LENGTHS = (40, 64)
_HEX = frozenset("0123456789abcdef")


class VisibilityReportError(RuntimeError):
    """The report could not be produced for a repository."""


def _is_object_id(token: str) -> bool:
    return len(token) in _CANDIDATE_OBJECT_ID_LENGTHS and set(token) <= _HEX


def _offences(
    raw_output: str, globs: Sequence[str]
) -> list[tuple[str, str, str]]:
    """Every ``(commit, path, blob)`` candidate record in a raw ``git log``.

    The stream is NUL-separated: a bare object id starts a commit, and each
    following ``:<oldmode> <newmode> <old blob> <new blob> <status>`` header is
    followed by one path (two for a rename or copy). A deletion carries a zero
    new blob, so the removed content is described by the old blob.

    Unlike the gate's parser this keeps EVERY record, including repeated
    ``(path, blob)`` pairs, because the count of refused commits needs the
    commit list rather than the deduplicated pairs.
    """
    tokens = raw_output.split("\0")
    commit = ""
    records: list[tuple[str, str, str]] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.lstrip("\n").startswith(":"):
            header = token.lstrip("\n").split()
            i += 1
            if len(header) < 5:
                continue
            _oldmode, _newmode, old_blob, new_blob, status = header[:5]
            blob = old_blob if status.startswith("D") else new_blob
            paths_in_record = 2 if status[:1] in ("R", "C") else 1
            for _ in range(paths_in_record):
                if i >= len(tokens):
                    break
                path = tokens[i].strip("\n")
                i += 1
                if not path or not any(fnmatch.fnmatch(path, g) for g in globs):
                    continue
                if blob:
                    records.append((commit, path, blob))
            continue
        candidate = token.strip("\n")
        if candidate and _is_object_id(candidate):
            commit = candidate
        i += 1
    return records


def _quote(value: str) -> str:
    """A YAML single-quoted scalar: safe for ``: ``, ``#``, ``*`` and quotes."""
    return "'" + value.replace("'", "''") + "'"


def _git_log(repo_dir: str, runner: str) -> str:
    """The raw diff stream for all commits reachable from every ref."""
    args = [
        runner, "-c", "core.quotePath=false", "log", "--all", "-m", "--root",
        "--raw", "--no-abbrev", "-z", "--format=%H",
    ]
    # A diagnostic the operator reads must not depend on the host locale.
    env = dict(os.environ, LC_ALL="C")
    try:
        proc = subprocess.run(
            args, cwd=repo_dir, capture_output=True, text=True, check=False,
            env=env,
        )
    except FileNotFoundError as exc:
        raise VisibilityReportError(f"{repo_dir}: {exc}") from exc
    except NotADirectoryError as exc:
        raise VisibilityReportError(f"{repo_dir}: not a directory") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()
        reason = detail[-1] if detail else f"{runner} log exited {proc.returncode}"
        raise VisibilityReportError(f"{repo_dir}: {reason}")
    return proc.stdout


def generate_report(
    repo_dir: str,
    *,
    globs: Sequence[str],
    indent: int = 8,
    runner: str = "git",
) -> str:
    """The report section for one repository (see the module docstring).

    ``globs`` are the candidate path globs, the same the visibility gate judges
    (``RepoConfig.visibility_candidate_globs``). ``indent`` is the column of the
    ``visibility_exceptions:`` key; the entries nest two and four columns
    further. Raises :class:`VisibilityReportError` when the repository cannot be
    read: a failure is never rendered as a clean repository.
    """
    glob_list = tuple(globs)
    if not glob_list:
        raise VisibilityReportError("no candidate globs given")
    if indent < 0:
        raise VisibilityReportError("indent must not be negative")

    records = _offences(_git_log(repo_dir, runner), glob_list)

    # The counted set: distinct commits that carried candidate content.
    commits: list[str] = []
    seen_commits: set[str] = set()
    pairs: list[tuple[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for commit, path, blob in records:
        if commit not in seen_commits:
            seen_commits.add(commit)
            commits.append(commit)
        key = (path, blob)
        if key not in seen_pairs:
            seen_pairs.add(key)
            pairs.append(key)

    lines = [f"# repo: {repo_dir}", f"# commits_refused={len(commits)}"]
    lines.extend(f"# commit {commit}" for commit in commits)
    if not pairs:
        return "\n".join(lines) + "\n"

    dash = " " * (indent + 2)
    lines.append(" " * indent + "visibility_exceptions:")
    for path, blob in sorted(pairs):
        lines.append(f"{dash}- path: {_quote(path)}")
        lines.append(f"{dash}  blob: {_quote(blob)}")
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ops_engine.modules.visibility_report",
        description=(
            "Print, per repository, the commits the P36-001 visibility gate "
            "would refuse: the commit, path and blob of every candidate-path "
            "history entry over all refs, and the number of refused commits. "
            "The visibility_exceptions block pastes unchanged into the "
            "repository's Layover configuration."
        ),
    )
    parser.add_argument("repo_dir", nargs="+", metavar="REPO_DIR")
    parser.add_argument(
        "--glob", dest="globs", action="append", required=True, metavar="GLOB",
        help="candidate path glob (repeatable; e.g. '.skillweave/**')",
    )
    parser.add_argument(
        "--indent", type=int, default=8, metavar="N",
        help="column of the visibility_exceptions key (default: 8)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    sections: list[str] = []
    for repo_dir in args.repo_dir:
        try:
            sections.append(
                generate_report(repo_dir, globs=args.globs, indent=args.indent)
            )
        except VisibilityReportError as exc:
            print(f"visibility report: {exc}", file=sys.stderr)
            return 2
    sys.stdout.write("\n".join(sections))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
