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
    assert declaration.get("schema") == 2
    assert declaration.get("package") == "ops_engine"
    assert declaration.get("semver") == "SemVer 2.0.0"


def _reachable_submodules():
    tree = ast.parse(SRC_INIT.read_text(encoding="utf-8"))
    submodules = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level and node.module:
            submodules.add(node.module.split(".")[0])
    return submodules


def test_name_absent_from_declaration_is_unpromised():
    declaration = _load_declaration()
    declared_names = {e["name"] for e in declaration["exports"]}
    actual_names = set(_actual_all())

    unpromised = actual_names - declared_names
    assert not unpromised, (
        "a name absent from the declaration is unpromised and must not be "
        f"promised via __all__: {sorted(unpromised)}"
    )

    reachable = _reachable_submodules()
    assert reachable.isdisjoint(actual_names), (
        "submodule names are absent from the declaration, hence unpromised, "
        f"and must not appear in __all__: {sorted(reachable & actual_names)}"
    )


_SRC_ROOT = Path(__file__).resolve().parent.parent / "src"


def _module_file(module: str) -> Path:
    """Resolve a dotted module name under src/ to its source file."""
    rel = Path(*module.split(".")) 
    return _SRC_ROOT / (str(rel) + ".py")


def _promised_method_signatures():
    """Return the method signatures the declaration promises, keyed by method.

    Each entry is ``(module, class_name, method_name, args, kwargs)`` where
    ``args`` is the ordered positional-arg names and ``kwargs`` the ordered
    keyword-only names, as written in the ```json``` declaration block.
    """
    declaration = _load_declaration()
    out = []
    for m in declaration.get("methods", []):
        out.append(
            (
                m["class"],
                m["module"],
                m["name"],
                list(m.get("args", [])),
                list(m.get("kwargs", [])),
            )
        )
    return out


def _code_method_signature(module: str, class_name: str, method_name: str):
    """Extract ``(args, kwargs)`` for ``Class.method`` from source via ast.

    Returns the ordered positional-arg names and keyword-only names of the
    method (``self``/``cls`` excluded). Raises ``AssertionError`` if the class
    or method is not found, so a name the declaration promises but the code
    lacks fails the gate rather than being silently skipped.
    """
    tree = ast.parse(_module_file(module).read_text(encoding="utf-8"))

    cls_node = next(
        (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name),
        None,
    )
    assert cls_node is not None, f"{module}.{class_name} not found in source"

    for node in cls_node.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method_name:
            args = [
                a.arg
                for a in node.args.posonlyargs + node.args.args
                if a.arg not in ("self", "cls")
            ]
            kwargs = [a.arg for a in node.args.kwonlyargs]
            return args, kwargs

    raise AssertionError(f"{module}.{class_name}.{method_name} not found in source")


def test_promised_method_signatures_match_code():
    """The declaration, not the code, is the source of the promised signatures.

    Each method promised in the ```json``` block's ``methods`` array is located
    in source via ``ast`` and its positional and keyword-only parameter names
    are compared to the declaration. A signature change in the code with a
    stale declaration therefore fails this test — in BOTH directions: a rename
    in code, or a rename in prose.

    Boundary: this gates ONLY the methods listed in the ``methods`` array (the
    mirror-destination methods with promised semantics). It is not a total
    signature contract for every method on every public class.
    """
    promised = _promised_method_signatures()
    assert promised, "declaration promises no methods; the signature gate is inert"

    for class_name, module, method_name, exp_args, exp_kwargs in promised:
        act_args, act_kwargs = _code_method_signature(module, class_name, method_name)
        assert act_args == exp_args, (
            f"{class_name}.{method_name} positional args drift: "
            f"code={act_args} declaration={exp_args}"
        )
        assert act_kwargs == exp_kwargs, (
            f"{class_name}.{method_name} keyword-only args drift: "
            f"code={act_kwargs} declaration={exp_kwargs}"
        )

