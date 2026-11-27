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


def _no_git(monkeypatch):
    monkeypatch.setattr(migration_runner, "_run_clone", lambda cmd: pytest.fail("git was started"))


def _git_says(monkeypatch, template):
    """Make the clone fail with ``template``, ``{url}`` being the URL git got."""
    seen = []

    def failing(cmd):
        seen.append(cmd[-2])
        stderr = template.replace("{url}", cmd[-2]).encode()
        raise subprocess.CalledProcessError(128, cmd, b"", stderr)

    monkeypatch.setattr(migration_runner, "_run_clone", failing)
    return seen


#: Everything that must not stand in a token or in the userinfo of a URL.
BAD = ["'", '"', "/", "\\", "@", "?", "#", " ", "\t", "\r\n", "\n", "\x01", "&", "\udcff"]


def _halves_absent(text):
    assert "dumhead" not in text and "dumtail" not in text


@pytest.mark.parametrize("mark", BAD, ids=repr)
@pytest.mark.parametrize(
    "url",
    [
        "http://u:${TOKEN}@127.0.0.1:1/org/repo.git",
        "http://${TOKEN}@127.0.0.1:1/org/repo.git",
        "http://127.0.0.1:1/org/repo.git?private_token=${TOKEN}",
        "http://127.0.0.1:1/t/${TOKEN}/repo.git",
        "https://github.com/org/repo.git",
    ],
    ids=["password", "username", "query", "path", "github"],
)
def test_a_token_outside_the_allowed_set_is_refused_unprinted(monkeypatch, caplog, url, mark):
    monkeypatch.setattr(
        migration_runner.os, "environ", {"LVC312_TOKEN": "dumhead" + mark + "dumtail"}
    )
    _no_git(monkeypatch)
    text = _failure(migration_runner.GitRepoSource(url, token_env_var="LVC312_TOKEN"), caplog)
    assert "LVC312_TOKEN" in text, "the operator must learn which value is broken"
    _halves_absent(text)


@pytest.mark.parametrize("mark", [m for m in BAD if m != "\udcff"], ids=repr)
@pytest.mark.parametrize(
    "url",
    [
        "http://u:dumhead{}dumtail@127.0.0.1:1/org/repo.git",
        "http://dumhead{}dumtail@127.0.0.1:1/org/repo.git",
        "https://dumhead{}dumtail:x-oauth-basic@127.0.0.1:1/org/repo.git",
    ],
    ids=["password", "username", "token-as-user"],
)
def test_a_literal_credential_outside_the_allowed_set_is_refused_unprinted(
    monkeypatch, caplog, url, mark
):
    _no_git(monkeypatch)
    text = _failure(migration_runner.GitRepoSource(url.format(mark)), caplog)
    assert "percent-encode" in text
    _halves_absent(text)


@pytest.mark.parametrize(
    "token",
    ["ghp_Ab1Cd2", "github_pat_11A_b", "glpat-x.y~z", "ab+cd=", "pa%40ss", "user:secret-pw"],
)
def test_an_ordinary_token_reaches_git_unchanged(monkeypatch, caplog, token):
    monkeypatch.setenv("LVC312_TOKEN", token)
    seen = _git_says(monkeypatch, "fatal: unable to access '{url}': refused")
    source = migration_runner.GitRepoSource(
        "http://${TOKEN}@127.0.0.1:1/org/repo.git", token_env_var="LVC312_TOKEN"
    )
    text = _failure(source, caplog)
    assert seen == [f"http://{token}@127.0.0.1:1/org/repo.git"]
    assert "unable to access 'http://127.0.0.1:1/org/repo.git'" in text
    assert token not in text


@pytest.mark.parametrize(
    "url, secret",
    [
        (f"http://u:{LITERAL}@127.0.0.1:1/org/repo.git", LITERAL),
        (f"https://{LITERAL}@127.0.0.1:1/org/repo.git", LITERAL),
        ("http://u:dum%40pass-77@127.0.0.1:1/org/repo.git", "dum@pass-77"),
    ],
    ids=["password", "token-only", "percent-decoded"],
)
def test_a_secret_git_repeats_on_its_own_is_scrubbed(monkeypatch, caplog, url, secret):
    _git_says(monkeypatch, "fatal: {url} rejected, value " + secret + " unknown")
    text = _failure(migration_runner.GitRepoSource(url), caplog)
    assert "rejected, value *** unknown" in text
    assert secret not in text


@pytest.mark.parametrize("password", ["o", "git", "repo"])
def test_a_short_password_leaves_words_readable(monkeypatch, caplog, password):
    _git_says(monkeypatch, f"fatal: unable to report: password {password} refused")
    source = migration_runner.GitRepoSource(f"http://u:{password}@127.0.0.1:1/org/xrepox.git")
    text = _failure(source, caplog)
    assert "unable to report: password *** refused" in text
    assert "127.0.0.1:1/org/xrepox.git" in text


def test_an_ssh_user_name_is_not_treated_as_a_secret(monkeypatch, caplog):
    _git_says(monkeypatch, "git@forge.example: Permission denied; is git installed?")
    text = _failure(migration_runner.GitRepoSource("ssh://git@forge.example/org/repo.git"), caplog)
    assert "is git installed?" in text
    assert "ssh://forge.example/org/repo.git" in text


def test_an_at_sign_in_an_http_path_must_be_encoded(monkeypatch, caplog):
    _no_git(monkeypatch)
    text = _failure(migration_runner.GitRepoSource("http://127.0.0.1:1/org/re@po.git"), caplog)
    assert "%40" in text and "po.git" not in text


def test_an_encoded_at_sign_in_a_path_reaches_git(monkeypatch, caplog):
    seen = _git_says(monkeypatch, "fatal: refused")
    url = "http://127.0.0.1:1/org/re%40po.git"
    assert url in _failure(migration_runner.GitRepoSource(url), caplog)
    assert seen == [url]


@pytest.mark.parametrize("prefix", ["", "file://"], ids=["path", "file-url"])
def test_a_local_repository_with_space_and_at_sign_still_clones(
    monkeypatch, caplog, tmp_path, prefix
):
    """No credential is involved, so neither the URL nor an unused token is judged."""
    repo = tmp_path / "with space" / "x@v2" / "repo"
    repo.mkdir(parents=True)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run(git + ["init", "-q", "-b", "main"], check=True)
    subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "init"], check=True)
    monkeypatch.setenv("LVC312_TOKEN", "two\nlines")
    url = f"{prefix}{repo}"
    text = _failure(
        migration_runner.GitRepoSource(url, token_env_var="LVC312_TOKEN"), caplog
    )
    assert f"does not exist in {url}@main" in text, "the clone itself must succeed"


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


@pytest.mark.parametrize("scheme", ["git", "helper::https"])
def test_a_credential_is_refused_where_git_has_no_use_for_it(monkeypatch, caplog, scheme):
    """``git://`` has no authentication; git decodes and prints what stands there."""
    _no_git(monkeypatch)
    url = f"{scheme}://u:dumhead%2Fdumtail@127.0.0.1:1/org/repo.git"
    text = _failure(migration_runner.GitRepoSource(url), caplog)
    assert "percent-encode" in text
    _halves_absent(text)


@pytest.mark.parametrize(
    "said, shown",
    [
        ("value user:secret-pw-long end", "value *** end"),
        ("password secret-pw-long end", "password *** end"),
        ("gluedXsecret-pw-longX end", "gluedX***X end"),
    ],
    ids=["whole-token", "password-part", "glued"],
)
def test_a_token_with_a_password_part_is_scrubbed_in_every_form(monkeypatch, caplog, said, shown):
    monkeypatch.setenv("LVC312_TOKEN", "user:secret-pw-long")
    _git_says(monkeypatch, said)
    source = migration_runner.GitRepoSource(
        "http://${TOKEN}@127.0.0.1:1/org/repo.git", token_env_var="LVC312_TOKEN"
    )
    assert shown in _failure(source, caplog)
