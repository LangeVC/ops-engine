"""Public surface contract: CONTRACT.md and ops_engine.__all__ must agree."""

import ast
import json
import re
from pathlib import Path

SRC_INIT = Path(__file__).resolve().parent.parent / "src" / "ops_engine" / "__init__.py"
CONTRACT_PATH = Path(__file__).resolve().parent.parent / "CONTRACT.md"


def _actual_all():
    tree = ast.parse(SRC_INIT.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("src/ops_engine/__init__.py defines no __all__")


def _load_declaration():
    text = CONTRACT_PATH.read_text(encoding="utf-8")
    match = re.search(r"```json\n(.*?)\n```", text, re.DOTALL)
    assert match, "CONTRACT.md must contain a ```json``` declaration block"
    return json.loads(match.group(1))


def test_exports_and_declaration_agree():
    declaration = _load_declaration()
    declared_names = {e["name"] for e in declaration["exports"]}
    actual_names = set(_actual_all())

    assert declared_names == actual_names, (
        "CONTRACT.md and ops_engine.__all__ disagree.\n"
        f"in CONTRACT.md but not __all__: {sorted(declared_names - actual_names)}\n"
        f"in __all__ but not CONTRACT.md: {sorted(actual_names - declared_names)}"
    )


def test_every_export_is_classified():
    declaration = _load_declaration()
    for entry in declaration["exports"]:
        assert entry.get("contract") is True, (
            f"export {entry['name']!r} is not classified as contract"
        )


def test_declaration_uses_supported_schema():
    declaration = _load_declaration()
    assert declaration.get("schema") == 1
    assert declaration.get("package") == "ops_engine"
    assert declaration.get("semver") == "SemVer 2.0.0"
