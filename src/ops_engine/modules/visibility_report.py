"""P36-004: a readiness report of refused commits per repository.

One command prints, per repository, the refused commits with path and blob SHA
and the count refused over all reachable commits, read-only. The output can be
pasted as exception entries of P36-001 without editing.

The report is the visibility gate's dry-run output: for every commit in the
repository's reachable history whose tree touches a candidate path (``globs``),
the commit, path and blob SHA are listed. The owner uses this to decide which
``(path, blob)`` pairs to accept (P36-006).
"""

import subprocess
from collections import Counter
from typing import Optional, Sequence

from ops_engine.modules.mirror import _OBJECT_ID, _parse_candidate_offences


def generate_report(
    repo_dir: str,
    *,
    globs: Optional[Sequence[str]] = None,
    runner: Optional[str] = None,
) -> str:
    """Print the refused commits with path and blob SHA, per repository.

    ``repo_dir`` is the repository root on disk. ``globs`` are the candidate
    path globs (same as ``RepoConfig.visibility_candidate_globs``). When
    ``globs`` is empty or None, the report is empty (no candidate paths means
    nothing is ever a candidate).

    Returns a YAML-shaped string whose exception lines (``path``, ``blob``) can
    be pasted directly into ``visibility_exceptions`` entries of the layover
    configuration. The format is::

        repo=<path> commits_refused=<count>
          - path: <candidate-path>
            blob: <full-sha>

    The first line per repository names the path and the total count of distinct
    commits that carried candidate content across all reachable history. Each
    subsequent block is one distinct ``(path, blob)`` pair.

    The function is read-only: it runs ``git log`` and never writes to the
    repository.
    """
    _runner = runner or "git"
    glob_list = tuple(globs) if globs else ()
    if not glob_list:
        return ""

    # Same history walk as MirrorHandler._candidate_offences: the whole
    # reachable history, merges against each parent, root commits included.
    args = [
        _runner, "-c", "core.quotePath=false", "log", "-m", "--root",
        "--raw", "--no-abbrev", "-z", "--format=%H", "HEAD",
    ]
    try:
        proc = subprocess.run(
            args, cwd=repo_dir, capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return f"# error: {_runner} not found\n"
    if proc.returncode != 0:
        return f"# error: git log failed (exit {proc.returncode})\n"

    offences = _parse_candidate_offences(proc.stdout, glob_list)

    # Deduplicate by (path, blob) — same as the gate: keep the newest commit.
    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[str, str, str]] = []
    for commit, path, blob in offences:
        if not blob:
            continue
        key = (path, blob)
        if key in seen:
            continue
        seen.add(key)
        pairs.append((commit, path, blob))

    if not pairs:
        return ""

    # Count distinct commits carrying candidate content
    commit_counts = Counter(c for c, _p, _b in pairs)

    lines: list[str] = []
    lines.append(f"repo={repo_dir} commits_refused={len(commit_counts)}")
    for commit, path, blob in sorted(pairs, key=lambda x: (x[1], x[2])):
        lines.append(f"  # {commit}")
        lines.append(f"  - path: {path}")
        lines.append(f"    blob: {blob}")

    return "\n".join(lines) + "\n"
