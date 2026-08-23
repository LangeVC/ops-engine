"""Layover consumption: every layover declares the contract names it consumes.

The declaration lives in docs/layover-consumption.md, next to each layover's
pin, and is machine-readable (a JSON block, mirroring CONTRACT.md).
"""

import json
import re
from pathlib import Path

CONSUMPTION_PATH = Path(__file__).resolve().parent.parent / "docs" / "layover-consumption.md"
CONTRACT_PATH = Path(__file__).resolve().parent.parent / "CONTRACT.md"

EXPECTED_LAYOVERS = [
    "lvc-ops",
    "capacium-ops",
    "elementeer-ops",
    "fusionaize-ops",
    "skillweave-ops",
]


def _load_declaration():
    text = CONSUMPTION_PATH.read_text(encoding="utf-8")
    match = re.search(r"```json\n(.*?)\n```", text, re.DOTALL)
    assert match, "docs/layover-consumption.md must contain a ```json``` declaration block"
    return json.loads(match.group(1))


def _load_contract_names():
    text = CONTRACT_PATH.read_text(encoding="utf-8")
    match = re.search(r"```json\n(.*?)\n```", text, re.DOTALL)
    assert match, "CONTRACT.md must contain a ```json``` declaration block"
    return {e["name"] for e in json.loads(match.group(1))["exports"]}


def test_declaration_uses_supported_schema():
    declaration = _load_declaration()
    assert declaration.get("schema") == 1
    assert declaration.get("package") == "ops_engine"
    assert "layovers" in declaration


def test_exactly_five_layovers_declared():
    declaration = _load_declaration()
    names = [l["name"] for l in declaration["layovers"]]
    assert sorted(names) == sorted(EXPECTED_LAYOVERS), (
        f"expected the five layovers {sorted(EXPECTED_LAYOVERS)}, got {names}"
    )


def test_every_layover_declares_a_pin_and_consumed_names():
    declaration = _load_declaration()
    for layover in declaration["layovers"]:
        assert "pin" in layover and layover["pin"], (
            f"layover {layover['name']!r} declares no pin"
        )
        consumed = layover.get("consumes")
        assert consumed, f"layover {layover['name']!r} declares no consumed names"


def test_every_consumed_name_is_a_contract_name():
    declaration = _load_declaration()
    contract_names = _load_contract_names()
    for layover in declaration["layovers"]:
        unknown = sorted(set(layover["consumes"]) - contract_names)
        assert not unknown, (
            f"layover {layover['name']!r} consumes names outside the contract: {unknown}"
        )
