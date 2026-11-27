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


@pytest.mark.parametrize("ending", ["\r\n", "\r", "\n", " "], ids=["crlf", "cr", "lf", "space"])
@pytest.mark.parametrize(
    "url",
    ["http://u:${TOKEN}@127.0.0.1:1/org/repo.git", "http://127.0.0.1:1/org/repo.git?t=${TOKEN}"],
    ids=["userinfo", "query"],
)
def test_whitespace_around_the_token_is_dropped(monkeypatch, caplog, url, ending):
    monkeypatch.setenv("LVC312_TOKEN", TOKEN + ending)
    seen = []

    def failing(cmd):
        seen.append(cmd[-2])
        raise subprocess.CalledProcessError(128, cmd, b"", b"fatal: refused")

    monkeypatch.setattr(migration_runner, "_run_clone", failing)
    text = _failure(migration_runner.GitRepoSource(url, token_env_var="LVC312_TOKEN"), caplog)
    assert seen == [url.replace("${TOKEN}", TOKEN)]
    assert TOKEN not in text


@pytest.mark.parametrize("gap", ["\r\n", "\n", " ", "\t", "\x01"], ids=["crlf", "lf", "space", "tab", "ctrl"])
def test_a_token_with_whitespace_inside_is_refused_unprinted(monkeypatch, caplog, gap):
    monkeypatch.setenv("LVC312_TOKEN", "dummy-head" + gap + "dummy-tail")
    monkeypatch.setattr(migration_runner, "_run_clone", lambda cmd: pytest.fail("git was started"))
    source = migration_runner.GitRepoSource(
        "http://u:${TOKEN}@127.0.0.1:1/org/repo.git", token_env_var="LVC312_TOKEN"
    )
    text = _failure(source, caplog)
    assert "LVC312_TOKEN" in text, "the operator must learn which value is broken"
    assert "dummy-head" not in text and "dummy-tail" not in text


@pytest.mark.parametrize(
    "mark", ["'", '"', "/", "@", "?", "#", " ", "\n", "\r"],
    ids=["quote", "dquote", "slash", "at", "question", "hash", "space", "lf", "cr"],
)
def test_a_literal_password_with_any_character_is_not_printed(caplog, mark):
    """Real git against a closed port: neither half of the password may appear."""
    password = "dumhead" + mark + "dumtail"
    source = migration_runner.GitRepoSource(f"http://u:{password}@127.0.0.1:1/org/repo.git")
    text = _failure(source, caplog)
    assert "127.0.0.1:1/org/repo.git" in text, "the repository must stay identifiable"
    assert "dumhead" not in text and "dumtail" not in text


def test_stderr_that_repeats_a_literal_password_is_scrubbed(monkeypatch, caplog):
    def echoing(cmd):
        stderr = f"fatal: bad url {cmd[-2]} (password {LITERAL}'x/y)".encode()
        raise subprocess.CalledProcessError(128, cmd, b"", stderr)

    monkeypatch.setattr(migration_runner, "_run_clone", echoing)
    source = migration_runner.GitRepoSource(f"http://u:{LITERAL}'x/y@127.0.0.1:1/org/repo.git")
    text = _failure(source, caplog)
    assert "bad url" in text
    assert LITERAL not in text


def test_the_missing_subpath_message_names_no_credential(monkeypatch, caplog):
    monkeypatch.setattr(migration_runner, "_run_clone", lambda cmd: None)
    source = migration_runner.GitRepoSource(f"http://u:{LITERAL}@127.0.0.1:1/org/repo.git")
    text = _failure(source, caplog)
    assert "does not exist" in text
    assert LITERAL not in text


@pytest.mark.parametrize("kind", ["failed", "timeout"])
def test_the_raised_error_keeps_no_handle_on_argv(monkeypatch, kind):
    def failing(cmd):
        if kind == "timeout":
            raise subprocess.TimeoutExpired(cmd, 1)
        raise subprocess.CalledProcessError(128, cmd, b"", b"")

    monkeypatch.setattr(migration_runner, "_run_clone", failing)
    source = migration_runner.GitRepoSource(f"http://u:{LITERAL}@127.0.0.1:1/org/repo.git")
    with pytest.raises(RuntimeError) as caught:
        with source.materialize():
            pass
    assert caught.value.__context__ is None and caught.value.__cause__ is None
