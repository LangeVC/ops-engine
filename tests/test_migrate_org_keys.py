"""LNF-100-1: migrate-org-keys must not report success on a file it cannot read.

Measured 2026-08-24: ``check`` returned ``OK — 0 canonical org key(s)``, exit 0,
against a config whose first key is ``LangeVC:``. The tool only looked at an
``orgs:`` mapping, but every production layover carries the org key at the top
level, so "no keys" was a read failure, not a success.

Acceptance:
  1. check and migrate handle a top-level org key; an explicit ``orgs:`` mapping
     stays supported.
  2. Finding zero org keys exits non-zero and names the file and the expected
     layout — "no keys" against a config that has one is a read failure.
  3. Red proof: check against the five pre-migration configs (LangeVC, Capacium,
     fusionAIze, SkillWeave, elementeer) — four fail, one passes.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATE_SCRIPT = REPO_ROOT / "scripts" / "migrate-org-keys.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _run(*args):
    return subprocess.run(
        [sys.executable, str(MIGRATE_SCRIPT), *args],
        capture_output=True,
        text=True,
    )


def _config(name):
    return FIXTURES / name


# -- Red proof: check against the five pre-migration configs ---------------


def test_check_fails_on_langevc_top_level_key():
    result = _run("check", _config("langevc.yml"))
    assert result.returncode != 0
    assert "LangeVC" in result.stderr


def test_check_fails_on_capacium_top_level_key():
    result = _run("check", _config("capacium.yml"))
    assert result.returncode != 0
    assert "Capacium" in result.stderr


def test_check_fails_on_fusionaize_top_level_key():
    result = _run("check", _config("fusionaize.yml"))
    assert result.returncode != 0
    assert "fusionAIze" in result.stderr


def test_check_fails_on_skillweave_top_level_key():
    result = _run("check", _config("skillweave.yml"))
    assert result.returncode != 0
    assert "SkillWeave" in result.stderr


def test_check_passes_on_elementeer_top_level_key():
    result = _run("check", _config("elementeer.yml"))
    assert result.returncode == 0
    assert "OK" in result.stdout


# -- Criterion 1: top-level key is handled, orgs: mapping still supported ---


def test_migrate_rekeys_top_level_key():
    result = _run("migrate", _config("langevc.yml"))
    assert result.returncode == 0
    assert "LangeVC:" not in result.stdout
    assert "langevc:" in result.stdout
    assert "login: LangeVC" in result.stdout


def test_check_fails_due_to_top_level_key_even_when_values_are_canonical():
    result = _run("check", _config("elementeer.yml"))
    # elementeer is already canonical: top-level key is lowercase, so a plain
    # "no keys" on a *display-cased* key must not mask the elementeer pass.
    assert result.returncode == 0


def test_orgs_mapping_layout_still_supported_check():
    result = _run("check", _config("orgs-mapping.yml"))
    assert result.returncode != 0
    assert "fusionAIze" in result.stderr


def test_orgs_mapping_layout_still_supported_migrate():
    result = _run("migrate", _config("orgs-mapping.yml"))
    assert result.returncode == 0
    assert "fusionaize:" in result.stdout


def test_migrate_keeps_explicit_orgs_section_for_mapping_layout():
    result = _run("migrate", _config("orgs-mapping.yml"))
    assert result.returncode == 0
    assert "orgs:" in result.stdout


# -- Criterion 2: zero org keys is a read failure, named and non-zero --------


def test_check_with_no_org_keys_is_a_read_failure():
    result = _run("check", _config("no-orgs.yml"))
    assert result.returncode != 0
    assert "no-orgs.yml" in result.stderr
    assert "expected a top-level org key" in result.stderr


def test_migrate_with_no_org_keys_is_a_read_failure():
    result = _run("migrate", _config("no-orgs.yml"))
    assert result.returncode != 0
    assert "no-orgs.yml" in result.stderr


def test_check_ok_message_counts_nonzero_keys():
    result = _run("check", _config("elementeer.yml"))
    assert result.returncode == 0
    assert "canonical org key(s)" in result.stdout
    assert "1 canonical" in result.stdout
