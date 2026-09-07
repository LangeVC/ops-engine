"""pin-drift-check: reads a layover register from a path, compares pins vs the
engine version.

The register is the calling organisation's data and arrives as a ``--layovers``
path. Two shapes are tested, mirroring ADP-008's missing-destination shape:

- **no register supplied** — the check reports it has nothing to check and
  exits zero (nothing to check is not an error);
- **a register that contradicts the engine version** — the check fails, names
  the declared pin and the engine version, and exits non-zero.

No test reads a path outside this repository: the register fixtures are written
into a throwaway temporary directory by each test, and the engine version is
read from this repository's own ``pyproject.toml``. The historical sibling-read
behaviour (reading each layover's ``pyproject.toml`` by relative path under the
operator's checkout) is gone.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "pin-drift-check.py"

import importlib.util

spec = importlib.util.spec_from_file_location("pin_drift_check", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

ENGINE_VERSION = mod.read_latest_version(REPO)


def _write_register(tmp_path: Path, pins: dict[str, str]) -> Path:
    """Write a register JSON declaring the given ``name -> pin`` map."""
    register = {
        "schema": 1,
        "package": "ops_engine",
        "layovers": [{"name": name, "pin": pin} for name, pin in pins.items()],
    }
    path = tmp_path / "register.json"
    path.write_text(json.dumps(register), encoding="utf-8")
    return path


def run_check(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_parse_semver_strips_v_prefix():
    assert mod.parse_semver("v2.1.0") == (2, 1, 0)
    assert mod.parse_semver("2.0.0") == (2, 0, 0)


def test_parse_register_reads_layover_names_and_pins():
    text = json.dumps(
        {
            "schema": 1,
            "package": "ops_engine",
            "layovers": [
                {"name": "a-ops", "pin": "2.0.0"},
                {"name": "b-ops", "pin": "v3.1.0"},
            ],
        }
    )
    layovers = mod.parse_register(text)
    assert [l["name"] for l in layovers] == ["a-ops", "b-ops"]
    assert [l["pin"] for l in layovers] == ["2.0.0", "v3.1.0"]


def test_parse_register_rejects_an_entry_without_name_or_pin():
    text = json.dumps(
        {"schema": 1, "package": "ops_engine", "layovers": [{"name": "a-ops"}]}
    )
    with pytest.raises(mod.LayoverRegisterError):
        mod.parse_register(text)


@pytest.mark.parametrize(
    "raw, reason",
    [
        # top-level list -> not a schema-1 object
        ("[]", "not a schema-1 object"),
        # bare string -> not a schema-1 object
        ('"just a string"', "not a schema-1 object"),
        # wrong schema value
        ('{"schema": 2, "package": "ops_engine", "layovers": []}',
         "unsupported schema"),
        # wrong package
        ('{"schema": 1, "package": "not_ops", "layovers": []}',
         "unexpected package"),
        # empty layovers list
        ('{"schema": 1, "package": "ops_engine", "layovers": []}',
         "no layovers declared"),
        # JSON that does not parse
        ("{not json", "invalid JSON"),
    ],
)
def test_present_but_malformed_register_is_a_named_refusal(tmp_path, raw, reason):
    """A present-but-malformed register is refused by name, exit non-zero, with
    no traceback — same shape as mirror-destination-propose.py's named refusal."""
    reg = tmp_path / "bad.json"
    reg.write_text(raw, encoding="utf-8")
    r = run_check(["--layovers", str(reg), "--repo", str(REPO)])
    assert r.returncode != 0, r.stdout
    assert "Traceback" not in r.stderr
    assert "ERROR" in r.stderr
    assert reason in r.stderr


def test_drift_for_flags_only_mismatched_pins():
    layovers = [
        {"name": "a-ops", "pin": ENGINE_VERSION},
        {"name": "b-ops", "pin": "3.0.0"},
    ]
    assert mod.drift_for(layovers, ENGINE_VERSION) == [("b-ops", "3.0.0")]


def test_drift_for_accepts_a_v_prefixed_declared_pin():
    layovers = [{"name": "a-ops", "pin": f"v{ENGINE_VERSION}"}]
    assert mod.drift_for(layovers, ENGINE_VERSION) == []


def test_no_register_supplied_reports_nothing_to_check_and_exits_zero():
    r = run_check(["--repo", str(REPO)])
    assert r.returncode == 0, r.stderr
    assert "nothing to check" in r.stdout


def test_register_matching_the_engine_version_exits_zero(tmp_path):
    register = _write_register(tmp_path, {"a-ops": ENGINE_VERSION})
    r = run_check(["--layovers", str(register), "--repo", str(REPO)])
    assert r.returncode == 0, r.stderr
    assert ENGINE_VERSION in r.stdout


def test_register_contradicting_the_engine_version_fails_and_names_both(tmp_path):
    # The 2026-09-07 shape: a register still declaring 3.0.0 while the engine
    # pins v3.4.0. The check must fail and name both versions.
    register = _write_register(tmp_path, {"a-ops": "3.0.0"})
    r = run_check(["--layovers", str(register), "--repo", str(REPO)])
    assert r.returncode != 0, r.stdout
    assert "3.0.0" in r.stderr
    assert ENGINE_VERSION in r.stderr
