"""pin-drift-check: per-layover pin, latest, and changed consumed names.

The check runs unattended and reports, for every layover, its pin, the latest
ops-engine version, and the consumed names that changed (were introduced) after
that pin.
"""

import importlib.util
import subprocess
import sys
import re
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

# Each layout's ops-engine pin is read from its own pyproject.toml. Paths are
# resolved the same way the sequence reads them: sibling repositories of the
# ops-engine worktree root.
PIN_READS = {
    "lvc-ops": Path("../lvc-ops/pyproject.toml"),
    "capacium-ops": Path("../../capacium/capacium-ops/pyproject.toml"),
    "elementeer-ops": Path("../../elementeer/elementeer-ops/pyproject.toml"),
    "fusionaize-ops": Path("../../fusionaize/fusionaize-ops/pyproject.toml"),
    "skillweave-ops": Path("../../skillweave/skillweave-ops/pyproject.toml"),
}

OPS_ENGINE_RE = re.compile(
    r'"ops-engine(?:\[[^\]]*\])?\s*@\s*git\+[^\s@]+@v?(\d+\.\d+\.\d+)"'
)


def _load_module():
    spec = importlib.util.spec_from_file_location("pin_drift_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()


def real_pin_from_pyproject(path: Path) -> str:
    """Return the ops-engine version a layover pins in its pyproject.toml.

    The dependency line is ``ops-engine[extra] @ git+...@v3.0.0``; the version
    carries a leading ``v`` that is dropped for comparison.
    """
    text = path.read_text(encoding="utf-8")
    m = OPS_ENGINE_RE.search(text)
    if not m:
        raise ValueError(f"no ops-engine pin in {path}")
    return m.group(1)


def declared_pin_map(doc_text: str) -> dict[str, str]:
    decl = mod.extract_json_fence(doc_text)
    return {l["name"]: l["pin"].lstrip("v") for l in decl["layovers"]}


def reachable_pyproject(repo_root: Path, rel: str) -> Path | None:
    """Resolve a sibling read path; return Path when present else None."""
    cand = (repo_root / rel).resolve()
    return cand if cand.is_file() else None


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


def test_removed_declared_report_diffs_all_names_between_tags():
    previous = {
        "QueueManager",
        "OpsEngineConfig",
        "ReleaseHandler",
        "MergeHandler",
        "NotificationHandler",
    }
    current_now = {
        "QueueManager",
        "OpsEngineConfig",
        "ReleaseHandler",
        "MirrorHandler",
        "NotificationHandler",
    }
    removed = mod.removed_declared_names(previous, current_now)
    assert removed == ["MergeHandler"]


def test_layovers_consuming_removed_maps_each_consumer_to_its_removed_names():
    layovers = [
        {
            "name": "lvc-ops",
            "pin": "2.0.0",
            "consumes": [
                "MergeHandler",
                "MirrorHandler",
                "NotificationHandler",
            ],
        },
        {
            "name": "capacium-ops",
            "pin": "2.1.2",
            "consumes": ["MigrationRunner", "ApplyResult"],
        },
        {
            "name": "elementeer-ops",
            "pin": "2.0.0",
            "consumes": ["MergeHandler", "NotificationHandler"],
        },
    ]
    removed = {"MergeHandler"}
    consumers = mod.layovers_consuming_removed(layovers, removed)
    assert consumers == [
        ("elementeer-ops", ["MergeHandler"]),
        ("lvc-ops", ["MergeHandler"]),
    ]


def test_removed_consumption_lines_reports_only_when_something_removed():
    previous = {
        "QueueManager",
        "OpsEngineConfig",
        "ReleaseHandler",
        "MergeHandler",
    }
    current_now = {"QueueManager", "OpsEngineConfig", "ReleaseHandler"}
    layovers = [
        {
            "name": "lvc-ops",
            "pin": "2.0.0",
            "consumes": ["QueueManager", "MergeHandler"],
        },
        {"name": "capacium-ops", "pin": "2.1.2", "consumes": ["ReleaseHandler"]},
    ]
    removed = {"MergeHandler"}
    lines = mod.removed_consumption_lines(layovers, previous, current_now)
    assert lines
    assert not set(previous) - set(current_now) - removed  # MergeHandler is the only removed
    # lvc-ops consumes the removed name, so its line flags it
    lvc = next(ln for ln in lines if ln.startswith("lvc-ops"))
    assert "MergeHandler" in lvc


def test_removed_consumption_lines_stay_quiet_when_internal_only_changed():
    previous = {
        "QueueManager",
        "OpsEngineConfig",
        "ReleaseHandler",
        "MergeHandler",
    }
    current_now = previous  # no declared name changed
    layovers = [
        {
            "name": "lvc-ops",
            "pin": "2.0.0",
            "consumes": ["QueueManager", "MergeHandler"],
        }
    ]
    assert mod.removed_consumption_lines(layovers, previous, current_now) == []


def test_removed_consumption_lines_quiet_when_last_tag_is_current():
    # previous == current: nothing removed, so no lines even if consumers exist
    previous = {
        "QueueManager",
        "OpsEngineConfig",
        "ReleaseHandler",
        "MergeHandler",
    }
    current_now = {
        "QueueManager",
        "OpsEngineConfig",
        "ReleaseHandler",
        "MergeHandler",
    }
    layovers = [
        {
            "name": "lvc-ops",
            "pin": "2.0.0",
            "consumes": ["QueueManager", "MergeHandler"],
        }
    ]
    assert mod.removed_consumption_lines(layovers, previous, current_now) == []


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


def test_declared_pins_match_the_real_pyproject_pins():
    """The document's declared pin equals the value in the layover's own pyproject.

    This is the WRITE-side guard for CFG-005: a mirror that goes stale (doc says
    2.2.0 while every layover resolves v3.0.0) must fail here. The read is each
    layover's own pyproject.toml, quoted below on failure.
    """
    doc_text = (REPO / "docs" / "layover-consumption.md").read_text(encoding="utf-8")
    declared = declared_pin_map(doc_text)
    failures = []
    for name in EXPECTED_LAYOVERS:
        cand = reachable_pyproject(REPO, PIN_READS[name].as_posix())
        if cand is None:
            failures.append(f"{name}: sibling pyproject unreachable from this runner")
            continue
        real = real_pin_from_pyproject(cand)
        if declared[name] != real:
            failures.append(f"{name}: doc declares {declared[name]} but {cand} pins v{real}")
    assert not failures, "\n".join(failures)
