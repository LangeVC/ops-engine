"""pin-drift-check: per-layover pin, latest, and changed consumed names.

The check runs unattended and reports, for every layover, its pin, the latest
ops-engine version, and the consumed names that changed (were introduced) after
that pin.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "pin-drift-check.py"

EXPECTED_LAYOVERS = [
    "capacium-ops",
    "elementeer-ops",
    "fusionaize-ops",
    "lvc-ops",
    "skillweave-ops",
]


def _load_module():
    spec = importlib.util.spec_from_file_location("pin_drift_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()


def test_parse_semver_strips_v_prefix():
    assert mod.parse_semver("v2.1.0") == (2, 1, 0)
    assert mod.parse_semver("2.0.0") == (2, 0, 0)


def test_version_gt():
    assert mod.version_gt("2.1.0", "2.0.0")
    assert not mod.version_gt("2.0.0", "2.0.0")
    assert not mod.version_gt("2.0.0", "2.1.0")


def test_parse_layovers_reads_declaration():
    text = (REPO / "docs" / "layover-consumption.md").read_text(encoding="utf-8")
    layovers = mod.parse_layovers(text)
    names = [l["name"] for l in layovers]
    assert sorted(names) == sorted(EXPECTED_LAYOVERS)
    for l in layovers:
        assert l["pin"]
        assert l["consumes"]


def test_parse_contract_reads_names():
    text = (REPO / "CONTRACT.md").read_text(encoding="utf-8")
    names = mod.parse_contract_names(text)
    assert len(names) == 34
    assert "MigrationRunner" in names
    assert "QueueManager" in names


def test_build_timeline_from_tagged_names():
    tagged = {
        "v0.1.0": {"QueueManager", "OpsEngineConfig"},
        "v2.0.0": {"QueueManager", "OpsEngineConfig", "ReleaseHandler"},
        "v2.1.0": {
            "QueueManager",
            "OpsEngineConfig",
            "ReleaseHandler",
            "MigrationRunner",
        },
    }
    current = {
        "QueueManager",
        "OpsEngineConfig",
        "ReleaseHandler",
        "MigrationRunner",
        "BrandNew",
    }
    timeline = mod.build_timeline_from_tagged_names(tagged, current, "2.2.0")
    assert timeline == {
        "QueueManager": "0.1.0",
        "OpsEngineConfig": "0.1.0",
        "ReleaseHandler": "2.0.0",
        "MigrationRunner": "2.1.0",
        "BrandNew": "2.2.0",
    }


def test_changed_consumed_names_flags_names_introduced_after_pin():
    timeline = {
        "QueueManager": "0.1.0",
        "ReleaseHandler": "2.0.0",
        "MigrationRunner": "2.1.0",
    }
    layover = {
        "name": "x-ops",
        "pin": "2.0.0",
        "consumes": ["QueueManager", "ReleaseHandler", "MigrationRunner"],
    }
    assert mod.changed_consumed_names(layover, timeline, "2.2.0") == ["MigrationRunner"]


def test_changed_consumed_names_empty_when_pin_is_current():
    timeline = {"MigrationRunner": "2.1.0", "QueueManager": "0.1.0"}
    layover = {
        "name": "x-ops",
        "pin": "2.1.2",
        "consumes": ["QueueManager", "MigrationRunner"],
    }
    assert mod.changed_consumed_names(layover, timeline, "2.2.0") == []


def test_check_runs_unattended_and_reports_pin_latest_changed():
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(REPO)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout
    latest = mod.read_latest_version(REPO)

    assert f"latest" in out
    assert latest in out
    for name in EXPECTED_LAYOVERS:
        assert name in out
    # Every layover in the current tree has a pin consistent with its
    # consumption, so the changed column reports no drift.
    for name in EXPECTED_LAYOVERS:
        line = next(ln for ln in out.splitlines() if ln.startswith(name))
        assert line.split()[-1] == "-"
