"""LNF-000-1 (LVC-238): canonical_org_key reads a field Forgejo actually sends.

The v3.0.0 function raised whenever ``owner.lower_name`` was absent, because it
was built against Forgejo's DATABASE schema (``user.lower_name`` column), not
against the webhook payload. Measured 2026-08-26 against git.langevc.com's live
API, the ``owner`` object carries no ``lower_name`` at all — it carries
``login``, ``username``, ``full_name``, etc. LVC-229's own evidence recorded
that the webhook delivers ``repository.full_name = "fusionaize/faigrid"`` —
already lowercase.

These tests run the fixture against the REAL captured payload committed at
``tests/fixtures/forgejo_push_payload.json``, so a regression back to a
schema-only field turns red against a payload, not against a docstring.
"""

import json
import re

import pytest

from ops_engine.config_loader import canonical_org_key

FIXTURE = "tests/fixtures/forgejo_push_payload.json"

# The ``owner`` keys Forgejo's live API actually sends (measured 2026-08-26,
# per LVC-238). ``lower_name`` is a schema column and is deliberately absent.
MEASURED_OWNER_KEYS = {
    "active",
    "avatar_url",
    "created",
    "description",
    "email",
    "followers_count",
    "following_count",
    "full_name",
    "html_url",
    "id",
    "is_admin",
    "language",
    "last_login",
    "location",
    "login",
    "login_name",
    "prohibit_login",
    "pronouns",
    "restricted",
    "source_id",
    "starred_repos_count",
    "username",
    "visibility",
    "website",
}


@pytest.fixture(scope="module")
def captured_payload() -> dict:
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def _repository(payload: dict) -> dict:
    return payload["repository"]


def test_fixture_owner_matches_measured_api_keys(captured_payload):
    """The committed fixture mirrors the measured API: no ``lower_name`` key."""
    owner = _repository(captured_payload)["owner"]
    assert "lower_name" not in owner
    assert set(owner) == MEASURED_OWNER_KEYS


def test_captured_fixture_is_a_real_forgejo_webhook(captured_payload):
    """The fixture is a push webhook with a repository/full_name the API sends."""
    repo = _repository(captured_payload)
    assert captured_payload["ref"].startswith("refs/heads/")
    assert "full_name" in repo and "owner" in repo


def test_canonical_org_key_derives_from_captured_full_name(captured_payload):
    """criterion 1 + 2: the key is derived from a field the API sends; the
    committed fixture drives the check rather than a schema or a docstring."""
    repo = _repository(captured_payload)
    assert canonical_org_key(repo) == "fusionaize"


def test_canonical_org_key_lowercases_display_case_full_name():
    """full_name may carry display case (e.g. fusionAIze); the key is lowercase."""
    repo = {
        "owner": {
            "login": "fusionAIze",
            "login_name": "fusionAIze",
            "username": "fusionaize",
            "full_name": "fusionAIze",
        },
        "name": "faigrid",
        "full_name": "fusionAIze/faigrid",
    }
    assert canonical_org_key(repo) == "fusionaize"


def test_canonical_org_key_falls_back_to_owner_username_without_full_name():
    """owner.username is a field the API sends and is already the canonical,
    lowercase handle — usable when full_name is absent."""
    repo = {
        "owner": {"login": "fusionAIze", "username": "fusionaize"},
        "name": "faigrid",
    }
    assert canonical_org_key(repo) == "fusionaize"


def test_canonical_org_key_fails_named_without_any_usable_field(
    captured_payload,
):
    """criterion 3: a payload with no usable owner field still fails with a
    NAMED error — never a silent fallback to a display-only field or a guess."""
    repo = {
        "owner": {"full_name": "fusionAIze", "login": "fusionAIze"},
        "name": "faigrid",
        "full_name": "A Single Display Name",  # no '/', so no org portion
    }
    with pytest.raises(ValueError, match=re.escape("cannot derive canonical org key")):
        canonical_org_key(repo)


def test_canonical_org_key_fails_named_without_repository_object():
    with pytest.raises(ValueError, match=re.escape("cannot derive canonical org key")):
        canonical_org_key({})
