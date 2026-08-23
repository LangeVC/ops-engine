#!/usr/bin/env bash
# Verify every layover pins the current engine release, the drift check is green,
# and the running bot answers healthily -- against the running service, not the file.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 - "$repo" <<'PY'
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

repo = Path(sys.argv[1])

version_re = re.compile(r'^version\s*=\s*"(\d+\.\d+\.\d+)"')

# Running-service endpoints, the public HTTPS hosts the ops bots answer on
# (reverse-proxied from the deploy host; see server Caddyfile in lvc-ops).
RUNNING_HOSTS = {
    "lvc-ops": "https://ops.langevc.com",
    "capacium-ops": "https://ops.capacium.xyz",
    "elementeer-ops": "https://ops.elementeer.xyz",
    "fusionaize-ops": "https://ops.fusionaize.com",
    "skillweave-ops": "https://ops.skillweave.xyz",
}

# elementeer and skillweave answer on Cloudflare origin certs (self-signed);
# the public edge terminates for the others. A running-service liveness
# probe must tolerate the origin cert without weakening the assertion itself:
# we only accept a 200 whose body says the service is ok.
_INSECURE_HOSTS = {"https://ops.elementeer.xyz", "https://ops.skillweave.xyz"}

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

def http_get(url: str) -> tuple[int, str]:
    ctx = None
    base = url.split("/health")[0]
    if base in _INSECURE_HOSTS:
        ctx = ssl._create_unverified_context()
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
        return r.status, r.read().decode("utf-8")

def verify_running_service(layovers: list[dict]) -> None:
    """Prove the running bot answers healthy -- the running service, not the pin file.

    The running `/health` endpoint reports status but not the ops-engine version.
    Proving the running service answers healthily is the verifiable half; the
    installed-version half is only reachable inside the running container (see
    docs/layover-repin-log.md `Running-service verification`).
    """
    skip = os.environ.get("PINS_CURRENT_SKIP_LIVE", "").strip()
    if skip.lower() in ("1", "true", "yes"):
        print("pins-current: live service verification skipped (PINS_CURRENT_SKIP_LIVE)")
        return
    for l in sorted(layovers, key=lambda x: x["name"]):
        host = RUNNING_HOSTS[l["name"]]
        try:
            status, body = http_get(host + "/health")
        except urllib.error.URLError as e:
            raise SystemExit(
                f"running-service: {l['name']!r} at {host} unreachable: {e}"
            )
        if status != 200 or '"ok"' not in body:
            raise SystemExit(
                f"running-service: {l['name']!r} at {host} unhealthy "
                f"(status {status}, body {body!r})"
            )
        print(f"running-service: {l['name']:<16} healthy at {host}")

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

    verify_running_service(layovers)

    print(f"pins-current: all five layovers pin {latest}, drift check green, running service healthy")

main()
PY
