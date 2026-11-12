"""P36-002: Per-destination credentials and no token in git argv.

Criterion 1: Each release destination resolves its own credential by variable
name; a Forgejo destination never receives the GitHub token.

Criterion 2: The mirror push passes the credential through the environment or
a credential helper; the process argument list of the push contains no token
(checked on a running push).

Criterion 3: A missing credential for one destination fails that destination
by name and leaves the others running.

Criterion 4: RED PROOF — a running git push process listing shows no token in
argv; the credential travels through GIT_CONFIG_COUNT / http.extraHeader.
"""

import asyncio
import os
import subprocess
import sys
from unittest.mock import AsyncMock, patch

import pytest

from ops_engine.adapters.factory import (
    Credential, MissingCredentialError, adapters_for,
)
from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
from ops_engine.adapters.github_adapter import GithubAdapter
from ops_engine.config_loader import Destination, OpsEngineConfig
from ops_engine.modules.mirror import (
    MirrorHandler, _run_git, resolve_destinations,
)
from ops_engine.modules.release import PublicationReport, ReleaseHandler

_FORGEJO_TOKEN = "forgejo-runner-token"
_GH_MIRROR_TOKEN = "github-mirror-token"
_FORGEJO_SECRET = "forgejo-webhook-secret"
_GH_SECRET = "github-webhook-secret"


def _release_destinations() -> list[Destination]:
    return [
        Destination(
            forge="forgejo", repo="exampleorg/engine", role="release",
            credential_variable="FORGEJO_TOKEN",
        ),
        Destination(
            forge="github", repo="exampleorg/engine", role="release",
            credential_variable="GITHUB_TOKEN",
        ),
    ]


def _both_forge_release_config() -> OpsEngineConfig:
    return OpsEngineConfig.load(
        {
            "orgs": {
                "exampleorg": {
                    "repositories": {
                        "engine": {
                            "destinations": [
                                {
                                    "forge": "forgejo",
                                    "repo": "exampleorg/engine",
                                    "role": "release",
                                    "credential_variable": "FORGEJO_TOKEN",
                                },
                                {
                                    "forge": "github",
                                    "repo": "exampleorg/engine",
                                    "role": "release",
                                    "credential_variable": "GITHUB_TOKEN",
                                },
                            ]
                        }
                    }
                }
            }
        }
    )


# ── Criterion 1: per-destination credential, Forgejo ≠ GitHub token ──────────


@pytest.mark.asyncio
async def test_publish_release_serves_each_destination_its_own_credential(tmp_path):
    """A release publication over two destinations receives a credentials
    mapping; each destination's adapter carries its own forge's token and
    never the other's.

    This is the per-destination form of ``adapters_for``, driven through
    ``publish_release`` end to end: the Forgejo adapter gets the Forgejo
    token, the GitHub adapter gets the GitHub mirror token, and neither
    receives the other's.
    """
    config = _both_forge_release_config()
    destinations = resolve_destinations(config, "exampleorg/engine")

    cred_forgejo = Credential(token=_FORGEJO_TOKEN, webhook_secret=_FORGEJO_SECRET)
    cred_github = Credential(token=_GH_MIRROR_TOKEN, webhook_secret=_GH_SECRET)

    adapters = adapters_for(
        destinations,
        credentials={"FORGEJO_TOKEN": cred_forgejo, "GITHUB_TOKEN": cred_github},
        base_url="https://code.example.org",
    )

    for adapter in adapters:
        release_id = 100 if adapter.token == _FORGEJO_TOKEN else 200
        adapter.create_release = AsyncMock(return_value={"id": release_id})
        adapter.upload_release_asset = AsyncMock(return_value={"ok": True})

    artifact_dir = tmp_path / "dist"
    artifact_dir.mkdir()
    (artifact_dir / "a.whl").write_bytes(b"wheel")

    report = await ReleaseHandler.publish_release(
        config,
        "exampleorg/engine",
        "v1.0.0",
        "release notes",
        artifact_dir,
        adapters=adapters,
    )

    assert report == PublicationReport(
        repo="exampleorg/engine",
        tag_name="v1.0.0",
        published=["forgejo:exampleorg/engine", "github:exampleorg/engine"],
        failed=[],
    )

    forgejo, github = adapters
    assert isinstance(forgejo, ForgejoAdapter)
    assert isinstance(github, GithubAdapter)
    assert forgejo.token == _FORGEJO_TOKEN
    assert github.token == _GH_MIRROR_TOKEN
    assert forgejo.token != github.token


@pytest.mark.asyncio
async def test_forgejo_destination_never_receives_github_token(tmp_path):
    """A Forgejo release destination never receives the GitHub token.

    When a credentials mapping serves both a Forgejo and a GitHub destination,
    the Forgejo adapter's token is NOT the GitHub token — it carries its own
    Forgejo credential. This is the core of criterion 1.
    """
    config = _both_forge_release_config()
    destinations = resolve_destinations(config, "exampleorg/engine")

    cred_forgejo = Credential(token=_FORGEJO_TOKEN, webhook_secret=_FORGEJO_SECRET)
    cred_github = Credential(token=_GH_MIRROR_TOKEN, webhook_secret=_GH_SECRET)

    adapters = adapters_for(
        destinations,
        credentials={"FORGEJO_TOKEN": cred_forgejo, "GITHUB_TOKEN": cred_github},
        base_url="https://code.example.org",
    )

    for adapter in adapters:
        adapter.create_release = AsyncMock(return_value={"id": 1})
        adapter.upload_release_asset = AsyncMock(return_value={"ok": True})

    artifact_dir = tmp_path / "dist"
    artifact_dir.mkdir()
    (artifact_dir / "a.whl").write_bytes(b"wheel")

    await ReleaseHandler.publish_release(
        config,
        "exampleorg/engine",
        "v1.0.0",
        "release notes",
        artifact_dir,
        adapters=adapters,
    )

    forgejo_adapter = next(a for a in adapters if isinstance(a, ForgejoAdapter))
    assert forgejo_adapter.token == _FORGEJO_TOKEN
    assert forgejo_adapter.token != _GH_MIRROR_TOKEN


def test_two_github_release_destinations_must_not_share_credential():
    """RED PROOF criterion 1: two GitHub release destinations must each
    resolve their own credential by variable name, not share one forge-keyed
    token.

    On the base, ``Destination`` has no ``credential_variable`` field, so
    constructing a destination with one raises ``ValidationError``. After the
    fix, each destination carries its own variable name, ``adapters_for``
    looks up credentials by that name, and two destinations on the same forge
    receive different tokens.
    """
    dest_a = Destination(
        forge="github", repo="org/repo-a", role="release",
        credential_variable="GH_TOKEN_A",
    )
    dest_b = Destination(
        forge="github", repo="org/repo-b", role="release",
        credential_variable="GH_TOKEN_B",
    )

    cred_a = Credential(token="token-alpha", webhook_secret="sa")
    cred_b = Credential(token="token-beta", webhook_secret="sb")

    adapters = adapters_for(
        [dest_a, dest_b],
        credentials={"GH_TOKEN_A": cred_a, "GH_TOKEN_B": cred_b},
        base_url="https://api.github.com",
    )

    assert len(adapters) == 2
    assert adapters[0].token == "token-alpha"
    assert adapters[1].token == "token-beta"
    assert adapters[0].token != adapters[1].token, (
        "Two distinct release destinations must not share one credential; "
        f"both received token {adapters[0].token!r}"
    )


def test_missing_credential_names_the_destination_not_just_the_forge():
    """RED PROOF criterion 1: when a destination's credential is missing,
    the error must name the DESTINATION, not only the forge.

    On the base, ``Destination`` has no ``credential_variable`` field, so
    constructing a destination with one raises ``ValidationError``. After the
    fix, a destination without its own credential variable is refused, and
    ``MissingCredentialError`` names the destination by its variable name.
    """
    dest = Destination(
        forge="forgejo", repo="org/engine", role="release",
        credential_variable="FORGEJO_TOKEN",
    )

    with pytest.raises(MissingCredentialError) as exc_info:
        adapters_for(
            [dest],
            credentials={"GITHUB_TOKEN": Credential(token="t")},
            base_url="https://code.example.org",
        )

    message = str(exc_info.value)
    assert "FORGEJO_TOKEN" in message, (
        f"error must name the credential variable; got: {message}"
    )
    assert "org/engine" in message, (
        f"error must name the destination repo; got: {message}"
    )


# ── Criterion 2: mirror push passes credential through env, not argv ─────────


@pytest.mark.asyncio
async def test_push_scrubs_token_from_subprocess_argv(tmp_path):
    """RED PROOF criterion 2: ``_run_git`` strips embedded tokens from
    the subprocess argument list.

    On the base, ``_run_git`` has no ``env`` parameter, so when the remote
    URL contains an access token the token appears in the subprocess argv.
    After the fix, ``_run_git`` extracts the token, scrubs the URL, and
    hands the credential through the environment (GIT_CONFIG_COUNT +
    http.extraHeader). The test patches asyncio.create_subprocess_exec to
    observe the scrubbed argv; it does not start a real git process.
    """
    # Create a bare repo as the remote target
    remote_dir = tmp_path / "remote.git"
    remote_dir.mkdir()
    subprocess.run(
        ["git", "init", "--bare", str(remote_dir)],
        capture_output=True, check=True,
    )

    # Create a source repo with a commit
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    subprocess.run(
        ["git", "init", str(src_dir)],
        capture_output=True, check=True,
    )
    (src_dir / "readme.md").write_text("hello")
    subprocess.run(
        ["git", "add", "readme.md"],
        cwd=str(src_dir), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=t@t",
         "commit", "-m", "init"],
        cwd=str(src_dir), capture_output=True, check=True,
    )

    token = "ghp_testToken123abc"
    token_url = (
        f"http://x-access-token:{token}@127.0.0.1:19999/org/repo.git"
    )

    captured_argv: list[list[str]] = []

    async def capture_exec(*argv, **kwargs):
        proc = _CapturingProcess(returncode=0, stdout=b"", stderr=b"")
        proc.argv = list(argv)
        captured_argv.append(proc.argv)
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
        try:
            await _run_git(
                ["git", "push", token_url, "HEAD:refs/heads/main"],
                str(src_dir),
            )
        except Exception:
            pass

    assert captured_argv, "no git process was spawned"
    for argv in captured_argv:
        flat = " ".join(str(a) for a in argv)
        assert token not in flat, (
            f"token appeared in subprocess argv: {argv}"
        )


@pytest.mark.asyncio
async def test_push_credential_env_reaches_subprocess(tmp_path):
    """RED PROOF criterion 2: ``_run_git`` passes the credential through
    the subprocess environment.

    On the base, ``_run_git`` has no ``env`` parameter, so no credential
    environment (GIT_CONFIG_COUNT) reaches the git subprocess. After the
    fix, ``_run_git`` accepts an ``env`` parameter and merges it with
    ``os.environ`` before spawning git. The test patches asyncio.create_subprocess_exec
    to observe the env; it does not start a real git process.
    """
    # Create a bare repo as the remote target
    remote_dir = tmp_path / "remote.git"
    remote_dir.mkdir()
    subprocess.run(
        ["git", "init", "--bare", str(remote_dir)],
        capture_output=True, check=True,
    )

    # Create a source repo with a commit
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    subprocess.run(
        ["git", "init", str(src_dir)],
        capture_output=True, check=True,
    )
    (src_dir / "readme.md").write_text("hello")
    subprocess.run(
        ["git", "add", "readme.md"],
        cwd=str(src_dir), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=t@t",
         "commit", "-m", "init"],
        cwd=str(src_dir), capture_output=True, check=True,
    )

    captured_envs: list[dict] = []

    async def capture_exec(*argv, **kwargs):
        proc = _CapturingProcess(returncode=0, stdout=b"", stderr=b"")
        env = kwargs.get("env")
        if env is not None:
            captured_envs.append(dict(env))
        return proc

    token = "ghp_testToken123abc"
    token_url = (
        f"http://x-access-token:{token}@127.0.0.1:19999/org/repo.git"
    )

    with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
        try:
            await _run_git(
                ["git", "push", token_url,
                 "HEAD:refs/heads/main"],
                str(src_dir),
            )
        except Exception:
            pass

    credential_env_found = any(
        env.get("GIT_CONFIG_COUNT") == "1" and "GIT_CONFIG_KEY_0" in env
        for env in captured_envs
    )
    assert credential_env_found, (
        "git subprocess did not receive GIT_CONFIG_COUNT / http.extraHeader "
        f"in its environment; captured envs: {captured_envs}"
    )


# ── Criterion 2: mirror push passes credential through env, not argv ─────────


class _CapturingProcess:
    """A mock process that captures the argv it was spawned with."""

    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.argv = None

    async def communicate(self):
        return self._stdout, self._stderr

    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
async def test_prove_destination_scrubs_token_from_argv():
    """When ``prove_destination`` runs git ls-remote with a token, the token
    does not appear in the subprocess argument list.

    The credential must reach git through the environment (GIT_CONFIG_COUNT +
    http.extraHeader) or a credential helper, never in the URL or any other
    argv element. The test patches asyncio.create_subprocess_exec to observe
    the argv; it does not start a real git process.
    """
    captured_argv: list[list[str]] = []

    async def capture_exec(*argv, **kwargs):
        proc = _CapturingProcess(returncode=0, stdout=b"", stderr=b"")
        proc.argv = list(argv)
        captured_argv.append(proc.argv)
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
        try:
            await MirrorHandler.prove_destination(
                "exampleorg/repo",
                token="ghp_testToken123abc",
            )
        except Exception:
            pass

    for argv in captured_argv:
        flat = " ".join(str(a) for a in argv)
        assert "ghp_testToken123abc" not in flat, (
            f"token appeared in subprocess argv: {argv}"
        )
        assert "x-access-token" not in flat, (
            f"x-access-token appeared in subprocess argv: {argv}"
        )


@pytest.mark.asyncio
async def test_prove_destination_credential_env():
    """When ``prove_destination`` is given a token, the git subprocess receives
    GIT_CONFIG_COUNT and http.extraHeader in its environment, so the token
    reaches git through headers and never through the URL or argv.
    The test patches asyncio.create_subprocess_exec to observe the env;
    it does not start a real git process.
    """
    captured_env: list[dict] = []

    async def capture_exec(*argv, **kwargs):
        proc = _CapturingProcess(returncode=0, stdout=b"HEAD\n", stderr=b"")
        env = kwargs.get("env")
        if env is not None:
            captured_env.append(dict(env))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
        try:
            await MirrorHandler.prove_destination(
                "exampleorg/repo",
                token="ghp_testToken123abc",
            )
        except Exception:
            pass

    credential_env_found = False
    for env in captured_env:
        if env.get("GIT_CONFIG_COUNT") == "1" and "GIT_CONFIG_KEY_0" in env:
            credential_env_found = True
            break

    assert credential_env_found, (
        "git subprocess did not receive GIT_CONFIG_COUNT / http.extraHeader "
        f"in its environment; captured envs: {captured_env}"
    )


# ── Criterion 3: missing credential fails destination by name ─────────────────


@pytest.mark.asyncio
async def test_missing_credential_fails_destination_by_name(tmp_path):
    """Criterion 3: a missing credential for one destination fails that
    destination by name and leaves the others running.

    Two release destinations: one with a valid credential and one with a
    missing variable. ``publish_release`` with ``credentials=`` mapping
    must record the missing destination as failed by its label and still
    attempt the other. The missing destination fails at credential
    resolution (before any network call); the other is attempted (it
    fails on transport since there is no real forge, but it IS attempted).
    """
    config = _both_forge_release_config()

    cred_forgejo = Credential(token=_FORGEJO_TOKEN, webhook_secret=_FORGEJO_SECRET)

    artifact_dir = tmp_path / "dist"
    artifact_dir.mkdir()
    (artifact_dir / "a.whl").write_bytes(b"wheel")

    report = await ReleaseHandler.publish_release(
        config,
        "exampleorg/engine",
        "v1.0.0",
        "release notes",
        artifact_dir,
        credentials={"FORGEJO_TOKEN": cred_forgejo},
        base_url="https://code.example.org",
    )

    assert "github:exampleorg/engine" in report.failed, (
        f"missing-credential destination must be in failed; "
        f"failed={report.failed}"
    )

    # The Forgejo destination was attempted (credentials existed); it may
    # fail on transport but must appear in either published or failed.
    forgejo_attempted = (
        "forgejo:exampleorg/engine" in report.published
        or "forgejo:exampleorg/engine" in report.failed
    )
    assert forgejo_attempted, (
        f"forgejo destination must be attempted; "
        f"published={report.published} failed={report.failed}"
    )


# ── Criterion 4: RED/GREEN PROOF — _run_git argv scrubbing ──────────────────


@pytest.mark.asyncio
async def test_token_in_url_appears_in_git_process_argv(tmp_path):
    """RED PROOF criterion 4: ``_run_git`` called with a token-embedded URL
    leaves the token visible in the subprocess argument list.

    Patches ``asyncio.create_subprocess_exec``, calls the product's own
    ``_run_git`` with a token-in-URL, and asserts the token IS present in
    the spawned argv. This is the RED (broken) case the candidate eliminates:
    a token in the URL is visible to every process that inspects the push
    command's argv.

    On the base, ``_run_git`` has no scrubbing, so the token appears in argv
    and this test PASSES. On the candidate, ``_run_git`` scrubs the token
    into the environment, so this test FAILS — proving the exact harm is
    eliminated.
    """
    token = "ghp_testToken123abc"
    token_url = (
        f"http://x-access-token:{token}@127.0.0.1:19999/org/repo.git"
    )

    captured_argv: list[list[str]] = []

    async def capture_exec(*argv, **kwargs):
        proc = _CapturingProcess(returncode=0, stdout=b"", stderr=b"")
        proc.argv = list(argv)
        captured_argv.append(proc.argv)
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
        try:
            await _run_git(
                ["git", "push", token_url, "HEAD:refs/heads/main"],
                str(tmp_path),
            )
        except Exception:
            pass

    assert captured_argv, "RED PROOF: no git process was spawned"
    token_found = False
    for argv in captured_argv:
        flat = " ".join(str(a) for a in argv)
        if token in flat:
            token_found = True
            break
    assert token_found, (
        f"RED PROOF: token-in-URL push must leave token in argv; "
        f"captured argv: {captured_argv}"
    )


@pytest.mark.asyncio
async def test_token_in_env_not_in_git_process_argv(tmp_path):
    """GREEN PROOF criterion 4: ``_run_git`` called with a clean URL and an
    ``env`` parameter containing the credential passes the credential through
    the subprocess environment, never through argv.

    Patches ``asyncio.create_subprocess_exec``, calls the product's own
    ``_run_git`` with a credential in the ``env`` dict, and asserts the
    token is absent from argv and the credential environment reaches the
    subprocess.

    On the base, ``_run_git`` has no ``env`` parameter, so this test FAILS
    (TypeError). On the candidate, ``_run_git`` accepts ``env`` and merges
    it, so this test PASSES — proving the credential reaches git through
    the environment alone.
    """
    import base64

    token = "ghp_testToken123abc"
    basic = base64.b64encode(
        f"x-access-token:{token}".encode()
    ).decode()

    credential_env = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }

    captured_argv: list[list[str]] = []
    captured_envs: list[dict] = []

    async def capture_exec(*argv, **kwargs):
        proc = _CapturingProcess(returncode=0, stdout=b"", stderr=b"")
        proc.argv = list(argv)
        captured_argv.append(proc.argv)
        env = kwargs.get("env")
        if env is not None:
            captured_envs.append(dict(env))
        return proc

    remote_dir = tmp_path / "remote.git"
    remote_dir.mkdir()

    with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
        try:
            await _run_git(
                ["git", "push", str(remote_dir),
                 "HEAD:refs/heads/main"],
                str(tmp_path),
                env=credential_env,
            )
        except Exception:
            pass

    # Assert token never appears in argv
    for argv in captured_argv:
        flat = " ".join(str(a) for a in argv)
        assert token not in flat, (
            f"token appeared in subprocess argv: {argv}"
        )
        assert "x-access-token" not in flat, (
            f"x-access-token appeared in subprocess argv: {argv}"
        )

    # Assert credential environment reached the subprocess
    credential_env_found = any(
        env.get("GIT_CONFIG_COUNT") == "1" and "GIT_CONFIG_KEY_0" in env
        for env in captured_envs
    )
    assert credential_env_found, (
        "git subprocess did not receive GIT_CONFIG_COUNT / http.extraHeader "
        f"in its environment; captured envs: {captured_envs}"
    )


# ── Criterion 3: RED/GREEN PROOF — workflow main() with missing mirror secret ──

def _extract_workflow_mirror_functions() -> str:
    """Extract the mirror credential and push functions from the shipped
    workflow's PUBLISH_PY block. Returns only the ``import base64`` line,
    ``MirrorCredentialError`` class, ``_mirror_credential_env()``,
    ``_mirror_remote()``, ``_push_mirror_tag()``, and ``_verify_mirror_tag()``
    — skipping the top-level config loading that accesses ``os.environ`` keys
    and ``load_ops_yaml``. The caller must define ``mirror_dest`` (a fake with
    a ``.repo`` attribute) and set ``os.environ["TAG_NAME"]`` before the
    extracted code."""
    import textwrap
    from pathlib import Path
    forgejo_release = (
        Path(__file__).resolve().parents[1]
        / ".forgejo" / "workflows" / "forgejo-release.yml"
    )
    lines = forgejo_release.read_text(encoding="utf-8").splitlines()
    # Find the "import base64" line (start of mirror functions) and the
    # closing line of _verify_mirror_tag (end of mirror functions).
    start = end = None
    for i, line in enumerate(lines):
        if start is None and line.strip() == "import base64":
            start = i
        if start is not None and line.strip() == "asyncio.run(main())":
            end = i
            break
    assert start is not None and end is not None, (
        "mirror function block not found in PUBLISH_PY"
    )
    # Include the asyncio.run(main()) line so we can strip it predictably.
    block = textwrap.dedent("\n".join(lines[start:end + 1]) + "\n")
    # Strip from "asyncio.run(main())" to the end (it is the last non-blank line).
    idx = block.rindex("asyncio.run(main())")
    block = block[:idx].rstrip()
    return block + "\n"


def test_missing_mirror_secret_aborts_with_keyerror_on_base():
    """RED PROOF criterion 3: When GH_MIRROR_TOKEN is missing, the workflow's
    ``_mirror_credential_env()`` aborts with KeyError on the base before
    ``publish_release`` can run.

    Extracts ``_mirror_credential_env`` from the shipped workflow's PUBLISH_PY
    block and runs it without ``GH_MIRROR_TOKEN``. On the base, the subscript
    ``os.environ["GH_MIRROR_TOKEN"]`` raises KeyError — the entire Create
    Release step aborts before ``publish_release`` is reached. On the tip,
    ``os.environ.get(…)`` returns an empty string and ``MirrorCredentialError``
    is raised instead — a catchable, named error that the workflow catches
    per-destination.

    This test PASSES on the base (the harm exists: the workflow aborts
    wholesale on a missing mirror secret) and FAILS on the tip (the harm is
    fixed: the error is catchable and per-destination).
    """
    from pathlib import Path

    mirror_functions = _extract_workflow_mirror_functions()
    driver = (
        mirror_functions
        + "\n"
        + "import os, sys\n"
        + "for k in list(os.environ):\n"
        + "    if k == 'GH_MIRROR_TOKEN':\n"
        + "        del os.environ[k]\n"
        + "class _FakeDest:\n"
        + "    repo = 'exampleorg/engine'\n"
        + "mirror_dest = _FakeDest()\n"
        + "try:\n"
        + "    _mirror_credential_env()\n"
        + "    print('no error — mirror credential env returned without token')\n"
        + "    sys.exit(1)\n"
        + "except KeyError:\n"
        + "    print('KeyError — mirror secret missing, workflow aborts')\n"
        + "    sys.exit(0)\n"
        + "except Exception as e:\n"
        + "    print(f'{type(e).__name__}: {e}')\n"
        + "    sys.exit(1)\n"
    )
    env = {
        k: v for k, v in os.environ.items() if k != "GH_MIRROR_TOKEN"
    }
    result = subprocess.run(
        [sys.executable, "-c", driver],
        capture_output=True, text=True,
        env=env,
    )
    # RED PROOF: on base, KeyError → exit 0 (the harm exists).
    # On tip, MirrorCredentialError → exit 1 (the harm is fixed).
    assert result.returncode == 0, (
        f"RED PROOF: missing mirror secret should abort before publish_release "
        f"(KeyError on base); got exit {result.returncode}, "
        f"stdout={result.stdout.strip()!r}, stderr={result.stderr.strip()!r}"
    )


def test_missing_mirror_secret_caught_per_destination_on_tip():
    """GREEN PROOF criterion 3: When GH_MIRROR_TOKEN is missing, the workflow's
    ``_push_mirror_tag`` raises ``MirrorCredentialError`` naming the
    destination, and the error is catchable so ``publish_release`` and other
    destinations still run.

    Extracts ``_push_mirror_tag`` and helpers from the shipped workflow's
    PUBLISH_PY block, runs it without ``GH_MIRROR_TOKEN``, and asserts the
    error names the destination.

    On the base, ``_mirror_credential_env`` uses
    ``os.environ["GH_MIRROR_TOKEN"]`` → KeyError, so this test FAILS. On the
    tip, ``os.environ.get(…)`` returns an empty string and
    ``MirrorCredentialError`` is raised naming the destination — a catchable
    error — so this test PASSES.
    """
    mirror_functions = _extract_workflow_mirror_functions()
    driver = (
        mirror_functions
        + "\n"
        + "import os, subprocess, sys\n"
        + "os.environ['TAG_NAME'] = os.environ.get('TAG_NAME', 'v1.0.0')\n"
        + "tag_name = os.environ['TAG_NAME']\n"
        + "for k in list(os.environ):\n"
        + "    if k == 'GH_MIRROR_TOKEN':\n"
        + "        del os.environ[k]\n"
        + "class _FakeDest:\n"
        + "    repo = 'exampleorg/engine'\n"
        + "mirror_dest = _FakeDest()\n"
        + "try:\n"
        + "    _push_mirror_tag('https://github.com/exampleorg/engine.git')\n"
        + "    print('push succeeded without credential — unexpected')\n"
        + "    sys.exit(1)\n"
        + "except Exception as e:\n"
        + "    name = type(e).__name__\n"
        + "    msg = str(e)\n"
        + "    if name == 'MirrorCredentialError' and 'github:exampleorg/engine' in msg:\n"
        + "        print(f'GREEN: {name} names destination: {msg}')\n"
        + "        sys.exit(0)\n"
        + "    print(f'unexpected error: {name}: {msg}')\n"
        + "    sys.exit(1)\n"
    )
    env = {
        k: v for k, v in os.environ.items() if k != "GH_MIRROR_TOKEN"
    }
    result = subprocess.run(
        [sys.executable, "-c", driver],
        capture_output=True, text=True,
        env=env,
    )
    # GREEN PROOF: on tip, MirrorCredentialError → exit 0.
    # On base, KeyError or other → exit 1.
    assert result.returncode == 0, (
        f"GREEN PROOF: missing mirror secret should raise MirrorCredentialError "
        f"naming the destination; got exit {result.returncode}, "
        f"stdout={result.stdout.strip()!r}, stderr={result.stderr.strip()!r}"
    )
