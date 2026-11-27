"""Bounded waiting for the git commands that depend on a remote answering.

``git ls-remote`` and ``git clone`` wait for as long as the other side keeps the
connection open. Both get a time limit, and both are killed together with their
helpers (``git-remote-https`` holds the connection and the output pipes) when the
limit passes or the caller is cancelled. Start them with :func:`start`.

They stay in the caller's process group, so a signal sent to that group reaches
them; the helpers are found by their parentage instead. A child that outlives a
git which has already exited is therefore not found.

Every other git command is started and awaited as before.
"""

import asyncio
import os
import signal
import subprocess
from typing import Optional

LS_REMOTE_TIMEOUT_SECONDS: float = 120
CLONE_TIMEOUT_SECONDS: float = 600

#: Exit code reported for a command that was killed at its limit (as ``timeout``).
TIMEOUT_EXIT_CODE = 124


def time_limit(argv: list) -> Optional[float]:
    """The limit in seconds for ``["git", <command>, ...]``, or ``None``."""
    command = argv[1] if len(argv) > 1 else ""
    if command == "ls-remote":
        return LS_REMOTE_TIMEOUT_SECONDS
    if command == "clone":
        return CLONE_TIMEOUT_SECONDS
    return None


def _parents() -> dict:
    """``{pid: parent pid}`` of every process, from ``/proc`` or else ``ps``."""
    table = {}
    if os.path.isdir("/proc/self"):
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat", "rb") as handle:
                    # "pid (command) state ppid ..."; the command may hold ")".
                    fields = handle.read().rpartition(b")")[2].split()
                table[int(name)] = int(fields[1])
            except (OSError, IndexError, ValueError):
                continue
        return table
    try:
        listing = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid="],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return table
    for line in listing.splitlines():
        pid, parent = line.split()
        table[int(pid)] = int(parent)
    return table


def _signal(pid: int, number: int) -> None:
    try:
        os.kill(pid, number)
    except (ProcessLookupError, PermissionError):
        pass


def kill_tree(pid: int) -> None:
    """Kill ``pid`` and its descendants; processes that are gone are fine.

    Each one is stopped before its children are looked for, so that none can
    start another while the tree is collected.
    """
    tree = [pid]
    _signal(pid, signal.SIGSTOP)
    while True:
        table = _parents()
        found = [c for c, p in table.items() if p in tree and c not in tree]
        if not found:
            break
        for child in found:
            _signal(child, signal.SIGSTOP)
        tree.extend(found)
    for member in tree:
        _signal(member, signal.SIGKILL)


async def _end(proc) -> None:
    # A process that was already reaped may have passed its pid on.
    if proc.returncode is None:
        kill_tree(proc.pid)
    await proc.wait()


async def start(argv: list, **kwargs):
    """``asyncio.create_subprocess_exec(*argv, **kwargs)``.

    For a bounded command a cancel that arrives while the process is starting is
    held back until it has started; then the whole tree is killed. Left to
    asyncio, only ``git`` itself would be killed.
    """
    if time_limit(argv) is None:
        return await asyncio.create_subprocess_exec(*argv, **kwargs)
    starting = asyncio.ensure_future(asyncio.create_subprocess_exec(*argv, **kwargs))
    cancelled = None
    while not starting.done():
        try:
            await asyncio.wait([starting])
        except asyncio.CancelledError as exc:
            cancelled = exc
    if cancelled is None:
        return starting.result()
    if starting.exception() is None:
        await _end(starting.result())
    raise cancelled


async def communicate(proc, argv: list) -> tuple:
    """``proc.communicate()`` within the limit of ``argv``.

    Returns ``(stdout, stderr, timed_out)``. A cancelled caller gets the
    ``CancelledError`` after the process tree has been killed and git reaped.
    """
    limit = time_limit(argv)
    if limit is None:
        out, err = await proc.communicate()
        return out, err, False
    try:
        out, err = await asyncio.wait_for(proc.communicate(), limit)
    except asyncio.TimeoutError:
        await _end(proc)
        return b"", b"", True
    except BaseException:
        await _end(proc)
        raise
    return out, err, False
