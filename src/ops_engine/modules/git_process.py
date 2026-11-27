"""Bounded waiting for the git commands that depend on a remote answering.

``git ls-remote`` and ``git clone`` wait for as long as the other side keeps the
connection open. Both get a time limit, and both are killed together with their
helpers (``git-remote-https`` holds the connection and the output pipes) when the
limit passes or the caller is cancelled. For that the command is started in its
own process group; pass :func:`spawn_options` to the call that starts it.

Every other git command is started and awaited as before.
"""

import asyncio
import os
import signal
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


def spawn_options(argv: list) -> dict:
    """Keyword arguments for the process start of ``argv``."""
    return {} if time_limit(argv) is None else {"start_new_session": True}


def kill_group(pid: int) -> None:
    """Kill the process group led by ``pid``; a group that is gone is fine."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def communicate(proc, argv: list) -> tuple:
    """``proc.communicate()`` within the limit of ``argv``.

    Returns ``(stdout, stderr, timed_out)``. A cancelled caller gets the
    ``CancelledError`` after the process group has been killed and reaped.
    """
    limit = time_limit(argv)
    if limit is None:
        out, err = await proc.communicate()
        return out, err, False
    try:
        out, err = await asyncio.wait_for(proc.communicate(), limit)
    except asyncio.TimeoutError:
        kill_group(proc.pid)
        await proc.wait()
        return b"", b"", True
    except BaseException:
        kill_group(proc.pid)
        await proc.wait()
        raise
    return out, err, False
