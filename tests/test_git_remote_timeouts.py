"""LVC-312: ``git ls-remote`` and ``git clone`` end on a remote that never answers.

The remote is a local TCP server that accepts the connection and sends nothing,
so real git (and its ``git-remote-http`` child) waits for ever. The server sees
EOF only when every process holding the connection is gone: that is the proof
that nothing survives a timeout or a cancel.
"""

import asyncio
import importlib
import socket
import threading

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
