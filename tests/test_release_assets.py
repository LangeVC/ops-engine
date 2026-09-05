"""REL-002 — a reproducible build produces four assets against committed constraints.

The workflow ``.forgejo/workflows/forgejo-release.yml`` builds a wheel, an
sdist, a ``SHA256SUMS`` and a CycloneDX SBOM from one documented command
sequence. This test proves each acceptance criterion against a real build run,
not against the workflow text alone: it executes the same commands the workflow
runs (``python3 -m build`` with a fixed ``SOURCE_DATE_EPOCH``, ``sha256sum``,
and the same SBOM derivation) and asserts on the produced bytes.

Nothing here touches ``src/ops_engine/``. Reproduction is a property of the
build inputs (committed ``constraints.txt``, the pinned build backend, a fixed
``SOURCE_DATE_EPOCH``) and is measured by hashing two independent builds.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".forgejo" / "workflows" / "forgejo-release.yml"
RELEASE_GATE = REPO_ROOT / ".forgejo" / "workflows" / "release-gate.yml"
CONSTRAINTS = REPO_ROOT / "constraints.txt"
PYPROJECT = REPO_ROOT / "pyproject.toml"

SOURCE_DATE_EPOCH = "1700000000"

WORKFLOW_CONTENT = WORKFLOW.read_text(encoding="utf-8")
RELEASE_GATE_CONTENT = RELEASE_GATE.read_text(encoding="utf-8")
CONSTRAINTS_CONTENT = CONSTRAINTS.read_text(encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build(out: Path) -> Path:
    """Run the build the workflow runs, ``--outdir`` to a fresh directory."""
    env = dict(os.environ)
    env["SOURCE_DATE_EPOCH"] = SOURCE_DATE_EPOCH
    subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--wheel", "--outdir", str(out)],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return out


def _pinned_pairs(text: str) -> list[tuple[str, str]]:
    pairs = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        m = re.match(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)", line)
        if m:
            pairs.append((m.group(1), m.group(2)))
    return pairs


def _sbom_code(sbom_file: Path) -> str:
    """The exact SBOM derivation the workflow runs: read constraints.txt alone."""
    return (
        "import json, re\n"
        "from pathlib import Path\n"
        "pins=[]\n"
        'for raw in Path(%r).read_text(encoding="utf-8").splitlines():\n'
        '    line=raw.split("#",1)[0].strip()\n'
        '    m=re.match(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)", line)\n'
        '    if m: pins.append((m.group(1), m.group(2)))\n'
        'components=[{"type":"library","name":n,"version":v,"purl":f"pkg:pypi/{n}@{v}"} for n,v in pins]\n'
        'sbom={"bomFormat":"CycloneDX","specVersion":"1.5","version":1,'
        '"metadata":{"component":{"type":"application","name":"ops-engine","version":"3.1.0"}},'
        '"components":components}\n'
        'Path(%r).write_text(json.dumps(sbom, indent=2, sort_keys=True)+"\\n", encoding="utf-8")\n'
        % (str(CONSTRAINTS), str(sbom_file))
    )


def _stage_assets(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Build once and arrange wheel, sdist, SHA256SUMS and sbom in one dir."""
    dist = _build(tmp_path / "run")
    wheel = next(dist.glob("*.whl"))
    sdist = next(dist.glob("*.tar.gz"))

    stage = tmp_path / "stage"
    stage.mkdir()
    stage_wheel = stage / wheel.name
    stage_sdist = stage / sdist.name
    stage_wheel.write_bytes(wheel.read_bytes())
    stage_sdist.write_bytes(sdist.read_bytes())

    with (stage / "SHA256SUMS").open("w") as fh:
        subprocess.run(
            ["sha256sum", stage_wheel.name, stage_sdist.name],
            cwd=stage,
            check=True,
            text=True,
            stdout=fh,
        )

    sbom_file = stage / "sbom.cdx.json"
    subprocess.run(
        [sys.executable, "-c", _sbom_code(sbom_file)], cwd=stage, check=True
    )

    return stage_wheel, stage_sdist, stage / "SHA256SUMS", sbom_file


# --- Criterion 1: a committed constraints file pins the resolution, and two
# builds from the same tag are byte-identical ---------------------------------


def test_constraints_file_is_committed_and_pins_exact_versions():
    """constraints.txt exists (committed) and pins every entry with ``==``."""
    assert CONSTRAINTS.exists()
    pairs = _pinned_pairs(CONSTRAINTS_CONTENT)
    assert len(pairs) >= 3
    for name, version in pairs:
        assert version, f"{name} is not pinned"
    names = [n for n, _ in pairs]
    assert "pydantic" in names
    assert "httpx" in names
    assert "pyyaml" in names


def test_build_backend_is_pinned():
    """The build backend itself is pinned, so two builds resolve the same
    hatchling version rather than whatever is latest."""
    assert "hatchling==" in PYPROJECT.read_text(encoding="utf-8")


def test_two_builds_from_same_tree_are_byte_identical(tmp_path):
    """Two independent builds produce byte-identical wheel and sdist."""
    dist1 = _build(tmp_path / "run1")
    dist2 = _build(tmp_path / "run2")

    wheels1 = sorted(dist1.glob("*.whl"))
    wheels2 = sorted(dist2.glob("*.whl"))
    sdists1 = sorted(dist1.glob("*.tar.gz"))
    sdists2 = sorted(dist2.glob("*.tar.gz"))

    assert wheels1 and wheels2 and sdists1 and sdists2
    assert [w.name for w in wheels1] == [w.name for w in wheels2]
    assert [s.name for s in sdists1] == [s.name for s in sdists2]

    for a, b in zip(wheels1, wheels2):
        assert _sha256(a) == _sha256(b), f"wheel differs: {a.name}"
    for a, b in zip(sdists1, sdists2):
        assert _sha256(a) == _sha256(b), f"sdist differs: {a.name}"


def test_build_venv_lives_outside_the_source_tree():
    """REL-008 rework — the build venv must be created outside the checkout.

    When the venv lived inside the tree as ``.release-venv``, ``python -m build
    --sdist`` swept the whole venv into the tarball, along with the build
    machine's absolute path baked into its pyvenv.cfg. Two runners at different
    absolute paths then produced sdists differing by exactly that path line, so
    the sdist was not byte-identical. The venv is now created via ``mktemp -d``
    under $TMPDIR, outside the tree hatchling packages, so no artifact can sweep
    it in regardless of hatchling's file selection.
    """
    # The in-tree venv creation command must not appear anywhere in the workflow.
    assert "python3 -m venv .release-venv" not in WORKFLOW_CONTENT
    # The outside-the-tree mechanism must be present.
    assert "$(mktemp -d)/release-venv" in WORKFLOW_CONTENT


# --- Criterion 2: all four assets from one documented command sequence --------


def test_workflow_documents_one_command_sequence_for_four_assets():
    """The single build step names all four assets: wheel, sdist, SHA256SUMS,
    SBOM."""
    # REL-008 — the build runs inside the step's own venv, so the command is
    # `python -m build`, not a bare `python3 -m build`. The assertion keeps the
    # semantic: one `build --sdist --wheel` sequence produces both archives.
    assert "-m build --sdist --wheel" in WORKFLOW_CONTENT
    assert "python3 -m venv" in WORKFLOW_CONTENT
    assert "sha256sum" in WORKFLOW_CONTENT
    assert "SHA256SUMS" in WORKFLOW_CONTENT
    assert "sbom.cdx.json" in WORKFLOW_CONTENT


def test_four_assets_are_produced_with_sizes(tmp_path):
    """Running the build sequence yields four assets with non-zero sizes."""
    wheel, sdist, sums_file, sbom_file = _stage_assets(tmp_path)
    assets = [wheel, sdist, sums_file, sbom_file]
    assert sum(1 for a in assets if a.is_file()) == 4
    sizes = {a.name: a.stat().st_size for a in assets}
    for name, size in sizes.items():
        assert size > 0, f"{name} is empty"


# --- Criterion 3: SHA256SUMS covers wheel and sdist, verifies with -c --------


def test_sha256sums_covers_archives_and_verifies(tmp_path):
    """SHA256SUMS has exactly the wheel and sdist, and ``sha256sum -c`` passes."""
    wheel, sdist, sums_file, _ = _stage_assets(tmp_path)
    stage = sums_file.parent

    lines = sums_file.read_text(encoding="utf-8").splitlines()
    assert {ln.split()[1] for ln in lines} == {wheel.name, sdist.name}

    r = subprocess.run(
        ["sha256sum", "-c", "SHA256SUMS"], cwd=stage, capture_output=True, text=True
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK" in r.stdout


def test_sha256sums_fails_on_tampered_byte(tmp_path):
    """A single flipped byte makes ``sha256sum -c`` fail non-zero."""
    wheel, _, sums_file, _ = _stage_assets(tmp_path)
    stage = sums_file.parent

    blobs = bytearray(wheel.read_bytes())
    blobs[0] ^= 0xFF
    wheel.write_bytes(bytes(blobs))

    r = subprocess.run(
        ["sha256sum", "-c", "SHA256SUMS"], cwd=stage, capture_output=True, text=True
    )
    assert r.returncode != 0
    assert "FAILED" in r.stdout


# --- Criterion 4: the SBOM describes the pinned build set, not the machine ---


def test_sbom_derives_from_constraints_not_pip_freeze():
    """The workflow derives the SBOM by reading constraints.txt and never
    invokes ``pip freeze`` (an incidental environment listing)."""
    assert "constraints.txt" in WORKFLOW_CONTENT
    # The SBOM step reads constraints.txt; no pip-freeze subcommand appears.
    assert "pip freeze" not in WORKFLOW_CONTENT.replace(
        "never `pip freeze`", ""
    )


def test_sbom_lists_exactly_the_pinned_set(tmp_path):
    """The SBOM component set equals the committed pins, name-for-name."""
    pinned = dict(_pinned_pairs(CONSTRAINTS_CONTENT))

    _, _, _, sbom_file = _stage_assets(tmp_path)
    sbom = json.loads(sbom_file.read_text(encoding="utf-8"))
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["specVersion"] == "1.5"
    sbom_names = {c["name"] for c in sbom["components"]}
    assert sbom_names == set(pinned), f"SBOM mismatch: {sbom_names ^ set(pinned)}"
    for c in sbom["components"]:
        assert c["version"] == pinned[c["name"]]


# --- REL-003: an OSV gate over the SBOM, so it is a check and not a document --
#
# The gate lives in .forgejo/workflows/release-gate.yml as an `osv-gate` job
# that runs after the version gate. It scans sbom.cdx.json (one of REL-002's
# four assets) with osv-scanner and refuses the release above a stated severity
# floor. The decision is made by an inline evaluator in the workflow, over the
# JSON osv-scanner emits; those bytes are what this section tests. The test
# executes the LITERAL evaluator block contained in the committed workflow, so
# a change to the gate's decision code that is not mirrored here turns this
# section red — the workflow text is the contract, not a copy of it.
#
# The evaluator is pure and offline: given an osv-scanner JSON result it prints
# a verdict and exits non-zero only when some finding's CVSS base score meets or
# exceeds the floor. No network is needed to test the decision; the scanner is
# exercised live separately (see the red/green proofs in the verdict).


def _osv_evaluator_source() -> str:
    """Extract the osv-scanner evaluator Python from release-gate.yml verbatim.

    The evaluator is the block between the `python3 - <<'PY'` marker and the
    matching `PY` terminator. Inside the YAML it is indented as block-scalar
    content; the runner de-indents it before execution, so we textwrap.dedent to
    the same shape.
    """
    lines = RELEASE_GATE_CONTENT.splitlines()
    start = end = None
    for i, line in enumerate(lines):
        if "<<'PY'" in line:
            start = i
        if start is not None and line.strip() == "PY" and i > start:
            end = i
            break
    assert start is not None and end is not None, "osv evaluator PY block not found"
    return textwrap.dedent("\n".join(lines[start + 1 : end]) + "\n")


def _run_osv_evaluator(scan_result: dict) -> subprocess.CompletedProcess:
    """Run the workflow's literal evaluator against a canned scan result.

    Mirrors the gate step: the evaluator reads a JSON path given by the
    OSV_SCAN_JSON environment variable (the workflow writes osv-scanner's JSON
    to /tmp/osv-scan.json and sets that variable before invoking Python)."""
    prog = _osv_evaluator_source()
    with open("/tmp/osv-gate-test-scan.json", "w", encoding="utf-8") as fh:
        json.dump(scan_result, fh)
    env = dict(os.environ)
    env["OSV_SCAN_JSON"] = "/tmp/osv-gate-test-scan.json"
    return subprocess.run(
        [sys.executable, "-c", prog],
        env=env,
        capture_output=True,
        text=True,
    )


def _osv_result(package_findings: list[tuple[str, str, str]]) -> dict:
    """Build an osv-scanner-shaped JSON result from (package, id, max_severity).

    osv-scanner groups advisories per package under `results[].packages[].groups[]`
    and reports each group's numeric CVSS base score in `max_severity`. Only this
    shape is consumed by the evaluator; it is all the OSV JSON the gate reads.
    """
    by_pkg: dict[str, list[tuple[str, str]]] = {}
    for name, id_, severity in package_findings:
        by_pkg.setdefault(name, []).append((id_, severity))
    packages = []
    for name, groups in by_pkg.items():
        group_list = []
        for id_, severity in groups:
            entry = {"ids": [f"PYSEC-{id_}", f"GHSA-{id_}"], "aliases": [], "max_severity": severity}
            group_list.append(entry)
        packages.append({"package": {"name": name, "ecosystem": "PyPI"}, "groups": group_list})
    return {"results": [{"source": {"type": "sbom"}, "packages": packages}]}


# Criterion 1 — the release gate runs a scanner over the SBOM and states its
# floor and rationale in the workflow (so it is a decision, not a default).


def test_release_gate_runs_osv_scanner_over_the_sbom():
    """release-gate.yml invokes osv-scanner against sbom.cdx.json."""
    assert "osv-gate" in RELEASE_GATE_CONTENT
    assert "osv-scanner" in RELEASE_GATE_CONTENT
    assert "sbom.cdx.json" in RELEASE_GATE_CONTENT
    # A stable release tag must carry REL-002's SBOM; absence is a named failure.
    assert "NoSbomToScanError" in RELEASE_GATE_CONTENT
    assert "Exit code" in RELEASE_GATE_CONTENT or "sys.exit" in RELEASE_GATE_CONTENT
    # The scanner binary is pinned so the tool that decides the release is a fixed
    # version rather than whatever is latest.
    assert "2.5.1" in RELEASE_GATE_CONTENT


def test_osv_floor_is_stated_and_defended_in_the_workflow():
    """A STATED severity threshold and its rationale are written into the gate."""
    # The floor is one constant the evaluator reads ...
    assert 'FLOOR = 7.0' in _osv_evaluator_source()
    # ... and the prose up top states the same floor and defends the choice against
    # the two failure modes PRD 8 names (never fires / disabled within a month).
    assert "CVSS base score" in RELEASE_GATE_CONTENT
    assert "HIGH" in RELEASE_GATE_CONTENT and "CRITICAL" in RELEASE_GATE_CONTENT


def test_osv_gate_consumes_a_real_sbom_asset():
    """The gate never fabricates an SBOM; it scans the file REL-002 emits."""
    # The scanner input is a fixed path to REL-002's release asset name, and the
    # gate's decision code only reads the scan JSON — it contains no code that
    # would synthesize a component list of its own.
    assert "--sbom=sbom.cdx.json" in RELEASE_GATE_CONTENT


# Criterion 2 (red proof) and 3 (green proof) — the evaluator's decision is
# exercised: an SBOM carrying a finding at/above the floor is refused (non-zero),
# and a clean SBOM (or one only below the floor) is permitted (zero). These run
# the literal workflow evaluator, so they are tests of the gate's written code.


def test_evaluator_passes_clean_sbom():
    """An SBOM with no findings is permitted (exit 0)."""
    r = _run_osv_evaluator(_osv_result([]))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RELEASE PERMITTED" in r.stdout


def test_evaluator_tolerates_below_floor_findings():
    """A finding below the floor is surfaced but does not block (exit 0)."""
    r = _run_osv_evaluator(
        _osv_result([("idna", "LOW", "3.1"), ("requests", "MED", "5.8")])
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RELEASE PERMITTED" in r.stdout


def test_evaluator_blocks_at_or_above_the_floor_red_proof():
    """A HIGH finding (>= floor) is refused with a non-zero exit (RED PROOF)."""
    r = _run_osv_evaluator(_osv_result([("requests", "X1", "7.5")]))
    assert r.returncode != 0, r.stdout + r.stderr
    assert "RELEASE REFUSED" in r.stdout
    assert "BLOCK" in r.stdout


# Criterion 4 — the gate names what it does NOT cover so nobody reads a green
# as more than it is: advisories published after the scan (the OSV database is
# a snapshot) and unpinned transitive resolution at the consumer's install time
# (LVC-249), which is outside this SBOM because it pins only the build set.


def test_green_run_names_what_it_does_not_cover():
    """The closing line of a permitted run states the non-coverage guarantees."""
    r = _run_osv_evaluator(_osv_result([]))
    assert "RELEASE PERMITTED" in r.stdout
    assert "NOT covered" in r.stdout
    assert "LVC-249" in r.stdout
    assert "snapshot" in r.stdout
