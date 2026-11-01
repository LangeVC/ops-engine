"""CONTRACT.md statements about the release path are pinned against the code.

Each test reads a statement of ``CONTRACT.md`` and compares it with the code
(signatures by ``inspect``, call structure by ``ast``, behaviour by a real call),
so a statement that drifts from the code, or a code change with no matching
statement, fails here. The statements added with the 3.5.0 release (the single
release path, the release reconcile entry point and the public-name count) fail
against a CONTRACT.md that lacks them.
"""

import ast
import asyncio
import inspect
import re
from pathlib import Path

import pytest

import ops_engine
from ops_engine.config_loader import ReconcileConfig, ReleaseConfig
from ops_engine.modules import reconcile as reconcile_module
from ops_engine.modules import release as release_module
from ops_engine.modules.reconcile import ReconcileHandler, ReconcileSinceError
from ops_engine.modules.release import ReleaseHandler
from ops_engine.modules.release_gates import (
    check_release_notes_audience,
    check_release_title,
    extract_changelog_section,
)

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = (ROOT / "CONTRACT.md").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".forgejo" / "workflows" / "forgejo-release.yml").read_text(
    encoding="utf-8"
)


def _section(heading: str) -> str:
    """The text of the ``## heading`` section of CONTRACT.md."""
    match = re.search(
        r"^## %s\n(.*?)(?=^## |\Z)" % re.escape(heading), CONTRACT, re.S | re.M
    )
    assert match, "CONTRACT.md has no '## %s' section" % heading
    return match.group(1)


def _rendered(func) -> str:
    """``name(a, b=None) -> ret`` as the contract writes a signature."""
    sig = inspect.signature(func)
    parts = []
    for param in sig.parameters.values():
        if param.default is inspect.Parameter.empty:
            parts.append(param.name)
        else:
            parts.append("%s=%r" % (param.name, param.default))
    text = "%s(%s)" % (func.__qualname__, ", ".join(parts))
    if sig.return_annotation is not inspect.Signature.empty:
        ret = sig.return_annotation
        if ret is None:
            ret = "None"
        text += " -> %s" % (ret if isinstance(ret, str) else ret.__name__)
    return text


def _calls(func_node: ast.AST) -> set:
    """Dotted names of everything called inside ``func_node``."""
    names = set()
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                names.add("%s.%s" % (target.value.id, target.attr))
            elif isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _function(module, name: str) -> ast.AST:
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError("%s has no function %s" % (module.__name__, name))


# --- the public-name count ---------------------------------------------------


def test_contract_states_the_number_of_public_names():
    stated = re.search(r"`ops_engine\.__all__` declares (\d+) names", CONTRACT)
    assert stated, "CONTRACT.md states no count of ops_engine.__all__"
    assert int(stated.group(1)) == len(ops_engine.__all__)
    assert re.search(
        r"`MirrorConfig` is one of the %d exports" % len(ops_engine.__all__), CONTRACT
    )


# --- the runtime identity and the release gates (checked against the code) ---


def test_runtime_identity_statement_matches_the_code():
    section = _section("Runtime identity")
    assert "get_runtime_identity() -> RuntimeIdentity" in section
    assert ops_engine.get_runtime_identity.__module__ == "ops_engine.runtime_identity"
    assert not inspect.signature(ops_engine.get_runtime_identity).parameters
    assert set(ops_engine.RuntimeIdentity.__dataclass_fields__) == {
        "version",
        "config_sha256",
    }
    for word in ("SHA-256", "config.yml", '"unknown"', "never raises"):
        assert word in section


@pytest.mark.parametrize(
    "func", [check_release_title, extract_changelog_section, check_release_notes_audience]
)
def test_release_gate_signatures_match_the_code(func):
    section = _section("Release gates")
    assert _rendered(func) in section
    assert getattr(ops_engine, func.__name__) is func


def test_release_gate_errors_are_named_and_are_value_errors():
    section = _section("Release gates")
    for name in (
        "ReleaseTitleError",
        "MissingChangelogEntryError",
        "ReleaseNotesAudienceError",
    ):
        assert "`%s`" % name in section
        assert issubclass(getattr(ops_engine, name), ValueError)
    assert "each error is a\n`ValueError`" in section


# --- the single release path --------------------------------------------------


def test_contract_names_the_single_release_function():
    section = _section("One release path")
    assert "`ReleaseHandler.publish_release`" in section
    assert "`ReleaseHandler.process_event`" in section
    assert "`.forgejo/workflows/forgejo-release.yml`" in section
    assert "MissingReleaseContextError" in section
    assert callable(ReleaseHandler.publish_release)


def test_webhook_handler_publishes_through_the_named_function():
    process_event = _function(release_module, "process_event")
    release_tag = _function(release_module, "_release_tag")
    assert "_release_tag" in _calls(process_event)
    calls = _calls(release_tag)
    assert "ReleaseHandler.publish_release" in calls
    # the order the statement gives: gates, then the identity, then publication
    lines = {}
    for node in ast.walk(release_tag):
        if isinstance(node, ast.Call):
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else "%s.%s" % (node.func.value.id, node.func.attr)
                if isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                else None
            )
            if name:
                lines.setdefault(name, node.lineno)
    assert (
        lines["check_release_title"]
        < lines["extract_changelog_section"]
        < lines["check_release_notes_audience"]
        < lines["push_canonical_tag"]
        < lines["ReleaseHandler.publish_release"]
    )


def test_tag_workflow_publishes_through_the_named_function():
    assert "await ReleaseHandler.publish_release(" in WORKFLOW
    assert "gates.check_release_title(" in WORKFLOW
    assert "gates.extract_changelog_section(" in WORKFLOW
    assert "scripts/release_notes_audience_gate.py" in WORKFLOW


def test_reconcile_publishes_through_the_webhook_path_not_a_second_one():
    reconcile = _function(reconcile_module, "reconcile_release_tags")
    assert "_release_tag" in _calls(reconcile)
    assert not hasattr(ReconcileHandler, "reconcile_releases")


# --- the release reconcile entry point ---------------------------------------


def test_contract_states_the_reconcile_entry_point_signature():
    section = _section("Release reconcile entry point")
    assert _rendered(ReconcileHandler.reconcile_release_tags) in section
    assert inspect.iscoroutinefunction(ReconcileHandler.reconcile_release_tags)
    assert "async` static method" in section
    assert list(
        inspect.signature(ReconcileHandler.reconcile_release_tags).parameters
    ) == [
        "adapter",
        "repo",
        "release_config",
        "context",
        "reconcile_config",
        "commit_date",
    ]


def test_contract_states_the_reconcile_switch_and_since():
    section = _section("Release reconcile entry point")
    assert "`ReleaseConfig.reconcile`" in section and "`release.reconcile`" in section
    assert ReleaseConfig().reconcile is True
    assert "`ReconcileConfig.since`" in section and "`reconcile.since`" in section
    assert "since" in ReconcileConfig.model_fields
    assert "ReconcileSinceError" in section
    assert "never called" in section


class _NoForgeCall:
    """An adapter that fails the test on any use."""

    def __getattr__(self, name):
        raise AssertionError("a forge call (%s) was made" % name)


def test_the_switch_off_reads_nothing():
    report = asyncio.run(
        ReconcileHandler.reconcile_release_tags(
            _NoForgeCall(),
            "org/repo",
            ReleaseConfig(enabled=True, reconcile=False),
            None,
            ReconcileConfig(since="v1.0.0"),
        )
    )
    assert report.published == [] and report.failed == {} and report.up_to_date == []


def test_a_missing_since_is_refused_before_any_forge_call():
    with pytest.raises(ReconcileSinceError, match=r"reconcile\.since"):
        asyncio.run(
            ReconcileHandler.reconcile_release_tags(
                _NoForgeCall(),
                "org/repo",
                ReleaseConfig(enabled=True),
                None,
                None,
            )
        )
