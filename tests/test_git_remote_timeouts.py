"""LVC-312: ``git ls-remote`` and ``git clone`` end on a remote that never answers.

The remote is a local TCP server that accepts the connection and sends nothing,
so real git (and its ``git-remote-http`` child) waits for ever. The server sees
EOF only when every process holding the connection is gone: that is the proof
that nothing survives a timeout, a cancel or a signal to the caller's group.
"""

import asyncio
import importlib
import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from ops_engine.modules import migration_runner, mirror, tag_identity

SHORT = 1.0
GUARD = 15.0


def _set_limit(monkeypatch, name, value):
    """Shorten a limit. Against code that has none, the test must fail by
    hanging past the guard, not on the missing module."""
    try:
        module = importlib.import_module("ops_engine.modules.git_process")
    except ModuleNotFoundError:
        return
    monkeypatch.setattr(module, name, value)


class _SilentRemote:
    """Accept one or more connections, read, never answer."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.url = "http://127.0.0.1:%d/org/repo.git" % self.sock.getsockname()[1]
        self.connected = threading.Event()
        self.hung_up = threading.Event()
        self._conns = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self._conns.append(conn)
            self.connected.set()
            threading.Thread(target=self._drain, args=(conn,), daemon=True).start()

    def _drain(self, conn):
        try:
            while conn.recv(4096):
                pass
        except OSError:
            pass
        self.hung_up.set()

    def close(self):
        for conn in self._conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
        self.sock.close()


@pytest.fixture
def remote(monkeypatch):
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    _set_limit(monkeypatch, "LS_REMOTE_TIMEOUT_SECONDS", SHORT)
    _set_limit(monkeypatch, "CLONE_TIMEOUT_SECONDS", SHORT)
    server = _SilentRemote()
    yield server
    server.close()


def _ended(coro):
    async def guarded():
        return await asyncio.wait_for(coro, GUARD)

    return asyncio.run(guarded())


@pytest.mark.parametrize("runner", [mirror._run_git, tag_identity._run_git], ids=["mirror", "tag_identity"])
@pytest.mark.parametrize("command", ["ls-remote", "clone"])
def test_runner_times_out_and_leaves_nothing(remote, tmp_path, runner, command):
    argv = ["git", command, remote.url]
    code, _out = _ended(runner(argv, str(tmp_path)))
    assert code == 124
    assert remote.connected.is_set(), "git never reached the remote"
    assert remote.hung_up.wait(5), "a git process still holds the connection"


def test_repo_exists_is_false_for_a_hanging_remote(remote):
    assert _ended(mirror.MirrorHandler._repo_exists(remote.url)) is False
    assert remote.hung_up.wait(5), "a git process still holds the connection"


@pytest.mark.parametrize("call", ["mirror", "tag_identity", "repo_exists"])
def test_cancel_kills_the_child(remote, tmp_path, monkeypatch, call):
    _set_limit(monkeypatch, "LS_REMOTE_TIMEOUT_SECONDS", 600)
    argv = ["git", "ls-remote", remote.url]

    async def scenario():
        coro = {
            "mirror": lambda: mirror._run_git(argv, str(tmp_path)),
            "tag_identity": lambda: tag_identity._run_git(argv, str(tmp_path)),
            "repo_exists": lambda: mirror.MirrorHandler._repo_exists(remote.url),
        }[call]()
        task = asyncio.ensure_future(coro)
        assert await asyncio.to_thread(remote.connected.wait, GUARD)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(remote.hung_up.wait, 5), "a git process survived the cancel"

    asyncio.run(scenario())


def test_migration_clone_times_out_without_naming_the_token(remote, monkeypatch):
    monkeypatch.setenv("LVC312_TOKEN", "s3cr3t-value")
    source = migration_runner.GitRepoSource(
        remote.url.replace("http://", "http://u:${TOKEN}@"), token_env_var="LVC312_TOKEN"
    )
    seen = []

    def clone():
        try:
            with source.materialize():
                pass
        except BaseException as exc:  # the thread hands the outcome to the test
            seen.append(exc)

    thread = threading.Thread(target=clone, daemon=True)
    thread.start()
    thread.join(GUARD)
    assert not thread.is_alive(), "the clone is still hanging"
    assert len(seen) == 1 and isinstance(seen[0], RuntimeError)
    assert "timed out" in str(seen[0])
    chain = "".join(str(e) for e in (seen[0], seen[0].__cause__) if e is not None)
    assert "s3cr3t-value" not in chain
    assert seen[0].__suppress_context__, "the traceback would print the argv with the token"
    assert remote.hung_up.wait(5), "a git process still holds the connection"


CALLS = {
    "mirror": "asyncio.run(mirror._run_git(['git', 'ls-remote', url], '.'))",
    "tag_identity": "asyncio.run(tag_identity._run_git(['git', 'ls-remote', url], '.'))",
    "repo_exists": "asyncio.run(mirror.MirrorHandler._repo_exists(url))",
    "migration": "migration_runner.GitRepoSource(url).materialize().__enter__()",
}


@pytest.mark.parametrize("call", sorted(CALLS))
def test_a_signal_to_the_callers_group_reaches_git(remote, tmp_path, call):
    program = textwrap.dedent(
        f"""
        import asyncio, sys
        from ops_engine.modules import migration_runner, mirror, tag_identity
        url = sys.argv[1]
        {CALLS[call]}
        """
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    caller = subprocess.Popen(
        [sys.executable, "-c", program, remote.url],
        cwd=tmp_path, env=env, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert remote.connected.wait(GUARD), "git never reached the remote"
        os.killpg(caller.pid, signal.SIGTERM)
        caller.wait(GUARD)
        assert remote.hung_up.wait(5), "git survived the signal to the caller's group"
    finally:
        if caller.poll() is None:
            os.killpg(caller.pid, signal.SIGKILL)


@pytest.mark.parametrize("call", ["mirror", "tag_identity", "repo_exists"])
def test_a_cancel_during_process_start_takes_the_helpers_along(
    remote, tmp_path, monkeypatch, call
):
    _set_limit(monkeypatch, "LS_REMOTE_TIMEOUT_SECONDS", 600)
    argv = ["git", "ls-remote", remote.url]

    async def scenario():
        loop = asyncio.get_running_loop()
        connect = loop.connect_read_pipe
        started = asyncio.Event()

        async def slow_connect(*args, **kwargs):
            # The child runs already; keep the start unfinished until its helper
            # holds the connection and the cancel has arrived.
            await asyncio.to_thread(remote.connected.wait, GUARD)
            started.set()
            await asyncio.sleep(0.5)
            return await connect(*args, **kwargs)

        monkeypatch.setattr(loop, "connect_read_pipe", slow_connect)
        if call == "repo_exists":
            # It pipes nothing; hold the start at the transport instead.
            make = loop._make_subprocess_transport

            async def slow_transport(*args, **kwargs):
                transport = await make(*args, **kwargs)
                try:
                    await slow_connect_gate()
                except BaseException:
                    transport.close()
                    raise
                return transport

            async def slow_connect_gate():
                await asyncio.to_thread(remote.connected.wait, GUARD)
                started.set()
                await asyncio.sleep(0.5)

            monkeypatch.setattr(loop, "_make_subprocess_transport", slow_transport)
        coro = {
            "mirror": lambda: mirror._run_git(argv, str(tmp_path)),
            "tag_identity": lambda: tag_identity._run_git(argv, str(tmp_path)),
            "repo_exists": lambda: mirror.MirrorHandler._repo_exists(remote.url),
        }[call]()
        task = asyncio.ensure_future(coro)
        await asyncio.wait_for(started.wait(), GUARD)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, GUARD)
        assert await asyncio.to_thread(remote.hung_up.wait, 5), "a helper survived the cancel"

    asyncio.run(scenario())


def test_a_timed_out_clone_ends_even_if_a_stray_child_holds_the_pipes(tmp_path, monkeypatch):
    """git is gone, a child of it keeps stdout open: the caller must not wait for it."""
    _set_limit(monkeypatch, "CLONE_TIMEOUT_SECONDS", 1.0)
    fake = tmp_path / "git"
    fake.write_text("#!/bin/sh\nsleep 30 &\nexit 0\n")
    fake.chmod(0o755)
    begun = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        migration_runner._run_clone([str(fake), "clone"])
    assert time.monotonic() - begun < GUARD
