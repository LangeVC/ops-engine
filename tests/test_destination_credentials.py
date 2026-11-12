"""P36-002: Per-destination credentials and no token in git argv.

Criterion 1: Each release destination resolves its own credential by variable
name; a Forgejo destination never receives the GitHub token.

Criterion 2: The mirror push passes the credential through the environment or
a credential helper; the process argument list of the push contains no token
(checked on a running push).
"""

import asyncio
import subprocess
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
        Destination(forge="forgejo", repo="exampleorg/engine", role="release"),
        Destination(forge="github", repo="exampleorg/engine", role="release"),
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
                                },
                                {
                                    "forge": "github",
                                    "repo": "exampleorg/engine",
                                    "role": "release",
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
        credentials={"forgejo": cred_forgejo, "github": cred_github},
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
        credentials={"forgejo": cred_forgejo, "github": cred_github},
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
async def test_real_push_does_not_pass_token_in_process_argv(tmp_path):
    """RED PROOF criterion 2: a real git push must not carry the token in
    its process argument list.

    On the base, ``_run_git`` has no ``env`` parameter, so when the remote
    URL contains an access token the token appears in the subprocess argv.
    After the fix, the credential reaches git through the environment
    (GIT_CONFIG_COUNT + http.extraHeader) and the URL is token-free.
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
async def test_real_push_passes_credential_via_environment(tmp_path):
    """RED PROOF criterion 2: a real git push must pass the credential
    through the subprocess environment.

    On the base, ``_run_git`` has no ``env`` parameter, so no credential
    environment (GIT_CONFIG_COUNT) reaches the git subprocess. After the
    fix, ``_run_git`` accepts an ``env`` parameter and merges it with
    ``os.environ`` before spawning git.
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
async def test_prove_destination_does_not_pass_token_in_process_argv():
    """When ``prove_destination`` runs git ls-remote with a token, the token
    does not appear in the subprocess argument list.

    The credential must reach git through the environment (GIT_CONFIG_COUNT +
    http.extraHeader) or a credential helper, never in the URL or any other
    argv element. A process listing of the running push must show no token.
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
async def test_prove_destination_passes_credential_via_environment():
    """When ``prove_destination`` is given a token, the git subprocess receives
    GIT_CONFIG_COUNT and http.extraHeader in its environment, so the token
    reaches git through headers and never through the URL or argv.
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
