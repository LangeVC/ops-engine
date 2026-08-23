#!/usr/bin/env bash
# Verify every layover pins the current engine release and the drift check is green.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 - "$repo" <<'PY'
import json
import re
import subprocess
import sys
from pathlib import Path

repo = Path(sys.argv[1])

version_re = re.compile(r'^version\s*=\s*"(\d+\.\d+\.\d+)"')

def current_release() -> str:
    text = (repo / "pyproject.toml").read_text(encoding="utf-8")
    for line in text.splitlines():
        m = version_re.search(line)
        if m:
            return m.group(1)
    raise SystemExit("current-release: no version found in pyproject.toml")

def load_layovers() -> list[dict]:
    text = (repo / "docs" / "layover-consumption.md").read_text(encoding="utf-8")
    m = re.search(r"```json\n(.*?)\n```", text, re.DOTALL)
    if not m:
        raise SystemExit("current-release: no json declaration block in docs/layover-consumption.md")
    decl = json.loads(m.group(1))
    return decl["layovers"]

def main() -> None:
    latest = current_release()
    layovers = load_layovers()

    expected = {"lvc-ops", "capacium-ops", "elementeer-ops", "fusionaize-ops", "skillweave-ops"}
    names = {l["name"] for l in layovers}
    assert names == expected, f"expected five layovers {sorted(expected)}, got {sorted(names)}"

    for l in layovers:
        pin = l["pin"]
        assert pin == latest, (
            f"layover {l['name']!r} pins {pin!r}, not the current release {latest!r}"
        )

    capacium = next(l for l in layovers if l["name"] == "capacium-ops")
    assert capacium.get("extra") == "postgres", (
        "capacium-ops [postgres] extra was dropped; it must be preserved as intended"
    )

    r = subprocess.run(
        [sys.executable, str(repo / "scripts" / "pin-drift-check.py"), "--repo", str(repo)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"pin-drift-check failed:\n{r.stderr}"
    out = r.stdout
    for l in layovers:
        line = next(ln for ln in out.splitlines() if ln.startswith(l["name"]))
        assert line.split()[-1] == "-", (
            f"drift check not green for {l['name']!r}: {line}"
        )

    print(f"pins-current: all five layovers pin {latest}, drift check green")

main()
PY
