"""LVC-312 follow-up: a failed migration clone names no credential.

``subprocess.CalledProcessError`` carries argv, and argv carries the tokenised
URL, so neither the message, the traceback nor a log line may repeat it.
"""

import logging
import subprocess
import traceback

import pytest

from ops_engine.modules import migration_runner

TOKEN = "dummy-token-4f1d"
LITERAL = "dummy-literal-9a2c"


def _failure(source, caplog) -> str:
    """Everything an operator could read about the failed clone."""
    caplog.set_level(logging.DEBUG)
    with pytest.raises(RuntimeError) as caught:
        with source.materialize():
            pass
    return "".join(traceback.format_exception(caught.value)) + caplog.text


def test_a_failed_clone_does_not_print_the_token(monkeypatch, caplog):
    monkeypatch.setenv("LVC312_TOKEN", TOKEN)
    source = migration_runner.GitRepoSource(
        "http://u:${TOKEN}@127.0.0.1:1/org/repo.git", token_env_var="LVC312_TOKEN"
    )
    text = _failure(source, caplog)
    assert "git clone failed" in text
    assert TOKEN not in text


@pytest.mark.parametrize(
    "url",
    ["https://github.com/org/repo.git", "https://forge.example/org/repo.git?t=${TOKEN}"],
    ids=["github-form", "token-outside-userinfo"],
)
def test_stderr_that_repeats_the_url_is_scrubbed(monkeypatch, caplog, url):
    monkeypatch.setenv("LVC312_TOKEN", TOKEN)

    def echoing(cmd):
        stderr = ("fatal: unable to access '%s': refused" % cmd[-2]).encode()
        raise subprocess.CalledProcessError(128, cmd, b"", stderr)

    monkeypatch.setattr(migration_runner, "_run_clone", echoing)
    text = _failure(migration_runner.GitRepoSource(url, token_env_var="LVC312_TOKEN"), caplog)
    assert "unable to access" in text, "the reason must survive the scrubbing"
    assert TOKEN not in text


def test_a_credential_written_into_the_url_is_not_printed(caplog):
    source = migration_runner.GitRepoSource(f"http://u:{LITERAL}@127.0.0.1:1/org/repo.git")
    text = _failure(source, caplog)
    assert "127.0.0.1:1/org/repo.git" in text, "the repository must stay identifiable"
    assert LITERAL not in text
