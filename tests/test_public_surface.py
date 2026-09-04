"""Public surface contract: CONTRACT.md and ops_engine.__all__ must agree."""

import ast
import importlib
import json
import re
import sys
import warnings
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
    """Return the method signatures the declaration promises, per method.

    Each entry is ``(module, class_name, method_name, sig)`` where ``sig`` is a
    dict with the keys ``kind``, ``args`` (ordered positional names),
    ``required_args`` (positional names with no default), ``kwargs`` (ordered
    keyword-only names), and ``required_kwargs`` (keyword-only names with no
    default), all as written in the ```json``` declaration block.
    """
    declaration = _load_declaration()
    out = []
    for m in declaration.get("methods", []):
        out.append(
            (
                m["class"],
                m["module"],
                m["name"],
                {
                    "kind": m.get("kind"),
                    "args": list(m.get("args", [])),
                    "required_args": list(m.get("required_args", [])),
                    "kwargs": list(m.get("kwargs", [])),
                    "required_kwargs": list(m.get("required_kwargs", [])),
                },
            )
        )
    return out


def _method_kind(node: ast.FunctionDef) -> str:
    """The method kind as a string: sync/async plus instance/static/class."""
    async_ = "async_" if isinstance(node, ast.AsyncFunctionDef) else ""
    for deco in node.decorator_list:
        name = None
        if isinstance(deco, ast.Name):
            name = deco.id
        elif isinstance(deco, ast.Attribute):
            name = deco.attr
        if name in ("staticmethod", "classmethod"):
            return f"{async_}{name}"
    return f"{async_}instance"


def _code_method_signature(module: str, class_name: str, method_name: str):
    """Extract the gated signature facts for ``Class.method`` from source.

    Returns a dict ``{kind, args, required_args, kwargs, required_kwargs}``
    where ``args``/``kwargs`` are the ordered positional / keyword-only names
    (``self``/``cls`` excluded) and ``required_*`` the subset of those names
    that carry no default. Raises ``AssertionError`` if the class or method is
    not found, so a name the declaration promises but the code lacks fails the
    gate rather than being silently skipped.
    """
    tree = ast.parse(_module_file(module).read_text(encoding="utf-8"))

    cls_node = next(
        (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name),
        None,
    )
    assert cls_node is not None, f"{module}.{class_name} not found in source"

    for node in cls_node.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method_name:
            all_pos = node.args.posonlyargs + node.args.args
            pos_names = [a.arg for a in all_pos if a.arg not in ("self", "cls")]
            n_defaults = len(node.args.defaults)
            required_args = pos_names[: len(pos_names) - n_defaults]

            kw_names = [a.arg for a in node.args.kwonlyargs]
            kw_defaults = node.args.kw_defaults or []
            required_kwargs = [
                a.arg
                for i, a in enumerate(node.args.kwonlyargs)
                if i >= len(kw_defaults) or kw_defaults[i] is None
            ]

            return {
                "kind": _method_kind(node),
                "args": pos_names,
                "required_args": required_args,
                "kwargs": kw_names,
                "required_kwargs": required_kwargs,
            }

    raise AssertionError(f"{module}.{class_name}.{method_name} not found in source")


def test_promised_method_signatures_match_code():
    """The declaration, not the code, is the source of the promised facts.

    Each method promised in the ```json``` block's ``methods`` array is located
    in source via ``ast`` and its ordered positional / keyword-only parameter
    names, required-ness, and kind are compared to the declaration. A change in
    any of those facts in the code with a stale declaration therefore fails this
    test — in BOTH directions: a change in code, or a change in prose.

    Boundary: this gates ONLY the facts named (names, required-ness, kind) for
    ONLY the methods listed in the ``methods`` array (the mirror-destination
    methods with promised semantics). Default values, type annotations, the
    method body, and ``*args``/``**kwargs`` are NOT gated, and neither are the
    semantic prose promises (case-sensitivity, check order, no network call
    before the double match). It is not a total signature contract for every
    method on every public class.
    """
    promised = _promised_method_signatures()
    assert promised, "declaration promises no methods; the signature gate is inert"

    for class_name, module, method_name, exp in promised:
        act = _code_method_signature(module, class_name, method_name)
        for key, label in (
            ("kind", "kind"),
            ("args", "positional args"),
            ("required_args", "required positional args"),
            ("kwargs", "keyword-only args"),
            ("required_kwargs", "required keyword-only args"),
        ):
            assert act[key] == exp[key], (
                f"{class_name}.{method_name} {label} drift: "
                f"code={act[key]} declaration={exp[key]}"
            )


def test_deprecated_variable_constants_warn_on_import(monkeypatch):
    """Importing the two deprecated variable-name constants emits a
    DeprecationWarning naming the removal version (4.0.0).

    The constants live in the unpromised submodule ``ops_engine.modules.mirror``
    and are served through module ``__getattr__`` precisely so that an importer
    — not an internal refusal path — trips the warning. This asserts the
    deprecation contract C1 names: both constants are deprecated, and the
    removal version is stated.
    """
    mirror = importlib.import_module("ops_engine.modules.mirror")

    for name in ("MIRROR_OWNER_VARIABLE", "MIRROR_REPO_VARIABLE"):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            value = getattr(mirror, name)
        assert value in ("GH_REPO_OWNER", "GH_REPO"), value
        assert caught, f"importing {name} emitted no DeprecationWarning"
        assert any(
            issubclass(w.category, DeprecationWarning) and "4.0.0" in str(w.message)
            for w in caught
        ), f"{name} warning must be a DeprecationWarning naming 4.0.0: {[str(w.message) for w in caught]}"


def _script_names():
    root = Path(__file__).resolve().parent.parent
    return {
        "audit": root / "scripts" / "mirror-destination-audit.py",
        "propose": root / "scripts" / "mirror-destination-propose.py",
    }


def test_operator_tools_do_not_import_the_action_variables():
    """DST-004: neither operator tool imports the two deprecated variable
    constants nor reads them on its primary path.

    The audit previously imported ``MIRROR_OWNER_VARIABLE`` /
    ``MIRROR_REPO_VARIABLE`` from ``ops_engine.modules.mirror`` (restating the
    Forgejo Actions variable read) and resolved each destination from the org /
    repo Actions variables. DST-004 made the org set arrive as repeated
    ``--config`` paths and the destination resolve from the config via
    ``resolve_destinations``. This is the READ that guards the write: we read
    the scripts as text and assert the deprecated-variable surface is gone from
    both, and that the config-first resolver import is present.
    """
    names = _script_names()
    for label, path in names.items():
        src = path.read_text(encoding="utf-8")
        assert "MIRROR_OWNER_VARIABLE" not in src, (
            f"{label} still imports/names MIRROR_OWNER_VARIABLE"
        )
        assert "MIRROR_REPO_VARIABLE" not in src, (
            f"{label} still imports/names MIRROR_REPO_VARIABLE"
        )
        assert "--config" in src, (
            f"{label} does not accept a --config path (org set at the call site)"
        )
    # the primary (propose) destination source is the Layer-1 pure resolver
    for label in ("audit", "propose"):
        src = names[label].read_text(encoding="utf-8")
        assert "resolve_destinations" in src, (
            f"{label} does not resolve through resolve_destinations"
        )
    # the audit is read-only and reads NO Forgejo Actions variable anywhere
    audit_src = names["audit"].read_text(encoding="utf-8")
    assert "/actions/variables/" not in audit_src, (
        "audit still reads a Forgejo Actions variable"
    )


def test_audit_selftest_runs_hermetic():
    """The audit's pure decision selftest exits 0 and never touches a forge.

    The class-5 refusal (malformed config github destination) is decided before
    any GitHub request; running the selftest is the hermetic proof of the pure
    decision functions that the live reachability reads build on.
    """
    import subprocess as _sp

    path = _script_names()["audit"]
    res = _sp.run(
        [sys.executable, str(path), "--selftest"],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "class 5" in res.stdout

