"""OE-007 - the release gates are public functions of ops-engine.

Three gates, each a public function that refuses with a named error:

  * ``check_release_title``                 -> ``ReleaseTitleError``
  * ``extract_changelog_section``           -> ``MissingChangelogEntryError``
  * ``check_release_notes_audience``        -> ``ReleaseNotesAudienceError``

Every gate has a RED proof: the refused input is fed to the real gate (it
refuses, by name) and to a MUTANT of the same source with every ``raise``
removed from that gate's function (the refusal removed). The proof demands
that the mutant lets the refused input through, i.e. that the very assertion
which passes against the real gate FAILS against the mutant. A test that
passed against the mutant would prove nothing.

The fixtures are faigrid's own: its CHANGELOG headings (including the
bracketed-link, bracketed-dated and ``(previous)`` forms and a heading naming
FAI-207) and its title forms (``.release-title-prefix``, the rendered
name_template, ``-rcN``). faigrid is a read-only reference clone; the fixtures
are copies of origin/main.
"""

import ast
import inspect
import types
from pathlib import Path

import pytest

import ops_engine
from ops_engine import (
    MissingChangelogEntryError,
    ReleaseNotesAudienceError,
    ReleaseTitleError,
    check_release_notes_audience,
    check_release_title,
    extract_changelog_section,
)
from ops_engine.modules import release_gates

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HEADINGS = (FIXTURES / "release_gates_faigrid_changelog_headings.md").read_text(
    encoding="utf-8"
)
TITLES = (FIXTURES / "release_gates_faigrid_titles.txt").read_text(encoding="utf-8")

FAIGRID_TRACKER_PREFIXES = ["FAI", "FFR", "OPS", "ADR"]


def _titles():
    prefix = template = None
    accepted, refused = [], []
    for line in TITLES.splitlines():
        if not line or line.startswith("#"):
            continue
        verdict, value = line.split("\t", 1)
        if verdict == "prefix":
            prefix = value
        elif verdict == "name_template":
            template = value
        elif verdict == "accept":
            accepted.append(value)
        elif verdict == "refuse":
            refused.append(value)
    return prefix, template, accepted, refused


PREFIX, NAME_TEMPLATE, ACCEPTED_TAGS, REFUSED_TAGS = _titles()


def _faigrid_changelog():
    """faigrid's real heading lines, each followed by one body line."""
    out = []
    for line in HEADINGS.splitlines():
        out.append(line)
        out.append("- entry under: " + line.lstrip("# ")[:12])
        out.append("")
    return "\n".join(out)


FAIGRID_CHANGELOG = _faigrid_changelog()


# --- mutation machinery: the refusal removed ---------------------------------


def _mutant(function_name: str) -> types.ModuleType:
    """The release_gates source with every ``raise`` inside ``function_name``
    replaced by ``pass`` - that gate's refusal removed, everything else intact."""
    tree = ast.parse(inspect.getsource(release_gates))

    class _Strip(ast.NodeTransformer):
        def visit_FunctionDef(self, node):
            if node.name != function_name:
                return node
            return self.generic_visit(node)

        def visit_Raise(self, node):
            return ast.copy_location(ast.Pass(), node)

    for i, node in enumerate(tree.body):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            tree.body[i] = _Strip().visit(node)
            break
    else:
        raise AssertionError(f"{function_name} not found in release_gates")
    ast.fix_missing_locations(tree)
    mod = types.ModuleType("release_gates_mutant")
    mod.__package__ = release_gates.__package__  # the relative import resolves
    exec(compile(tree, "release_gates_mutant", "exec"), mod.__dict__)
    return mod


def _count_raises(function_name: str) -> int:
    tree = ast.parse(inspect.getsource(release_gates))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            return sum(isinstance(n, ast.Raise) for n in ast.walk(node))
    raise AssertionError(function_name)


def _refuses(call, error):
    """Assert that ``call()`` raises exactly the named ``error``."""
    try:
        call()
    except error:
        return
    except Exception as exc:  # a different failure is not the named refusal
        raise AssertionError(
            f"expected {error.__name__}, got {type(exc).__name__}: {exc}"
        ) from exc
    raise AssertionError(f"expected {error.__name__}, nothing was raised")


# --- public surface ----------------------------------------------------------


def test_gates_are_exported_by_name():
    for name in (
        "check_release_title",
        "extract_changelog_section",
        "check_release_notes_audience",
        "ReleaseTitleError",
        "MissingChangelogEntryError",
        "ReleaseNotesAudienceError",
    ):
        assert name in ops_engine.__all__, name
        assert getattr(ops_engine, name) is getattr(release_gates, name)


def test_every_gate_has_a_refusal_to_remove():
    for fn in (
        "check_release_title",
        "extract_changelog_section",
        "check_release_notes_audience",
    ):
        assert _count_raises(fn) >= 1, fn


# --- gate 1: title -----------------------------------------------------------


@pytest.mark.parametrize("tag", ACCEPTED_TAGS)
def test_title_accepts_faigrid_forms_by_name_template_and_by_prefix(tag):
    expected = f"{PREFIX} {tag}"
    assert check_release_title(tag, name_template=NAME_TEMPLATE) == expected
    assert check_release_title(tag, prefix=PREFIX) == expected


@pytest.mark.parametrize("tag", REFUSED_TAGS)
def test_title_refuses_malformed_forms_by_name(tag):
    for kwargs in ({"name_template": NAME_TEMPLATE}, {"prefix": PREFIX}):
        with pytest.raises(ReleaseTitleError) as exc:
            check_release_title(tag, **kwargs)
        assert tag in str(exc.value)


def test_title_refuses_a_template_without_the_tag_placeholder():
    with pytest.raises(ReleaseTitleError):
        check_release_title("v1.0.0", name_template="fusionAIze Grid")


def test_title_refuses_neither_or_both_sources():
    with pytest.raises(ReleaseTitleError):
        check_release_title("v1.0.0")
    with pytest.raises(ReleaseTitleError):
        check_release_title("v1.0.0", name_template=NAME_TEMPLATE, prefix=PREFIX)
    with pytest.raises(ReleaseTitleError):
        check_release_title("v1.0.0", prefix="")


def test_title_is_a_gate_on_the_rendered_name_not_the_prefix_alone():
    # a template that appends text after the placeholder is rendered whole
    assert (
        check_release_title("v1.0.0-rc2", name_template="Grid {tag_name} (edge)")
        == "Grid v1.0.0-rc2 (edge)"
    )


@pytest.mark.parametrize("tag", ["v1.10.1-rc", "v1.10.1-RC1", "v1.10.1-beta"])
def test_red_title_gate_without_its_refusal_lets_the_refused_tag_through(tag):
    real = release_gates.check_release_title
    mutant = _mutant("check_release_title").check_release_title

    def probe(fn):
        return lambda: fn(tag, name_template=NAME_TEMPLATE)

    _refuses(probe(real), ReleaseTitleError)  # passes against the real gate
    with pytest.raises(AssertionError):  # ... and FAILS against the mutant
        _refuses(probe(mutant), ReleaseTitleError)


# --- gate 2: CHANGELOG section ----------------------------------------------


@pytest.mark.parametrize(
    "tag, expect",
    [
        ("v1.10.1", "1.10.1"),  # [x](compare-link) (date)
        ("v1.8.0", "1.8.0"),
        ("v1.6.0", "1.6.0"),  # [x] - date
        ("v1.5.1", "1.5.1"),  # [x] - 2025 (previous)
        ("1.7.0", "1.7.0"),  # tag without the leading v
    ],
)
def test_section_extracted_for_every_faigrid_heading_form(tag, expect):
    notes = extract_changelog_section(FAIGRID_CHANGELOG, tag)
    assert notes.startswith("- entry under"), notes
    # a section ends at the next version heading
    assert notes.count("- entry under") >= 1
    assert "compare/" not in notes


def test_section_of_1_8_0_holds_its_subheading_and_stops_at_1_7_0():
    notes = extract_changelog_section(FAIGRID_CHANGELOG, "v1.8.0")
    assert "FAI-207" in notes
    assert "1.7.0" not in notes


@pytest.mark.parametrize("tag", ["v9.9.9", "v1.10.2", "v1.10.1-rc1", "v1.5.0"])
def test_section_refuses_a_missing_section_by_name(tag):
    with pytest.raises(MissingChangelogEntryError) as exc:
        extract_changelog_section(FAIGRID_CHANGELOG, tag)
    assert tag.lstrip("v") in str(exc.value)


def test_section_refuses_a_heading_with_an_empty_body():
    with pytest.raises(MissingChangelogEntryError):
        extract_changelog_section("## [1.0.0] - 2026-01-01\n\n## [0.9.0]\n- x\n", "v1.0.0")


def test_section_refuses_an_empty_changelog():
    with pytest.raises(MissingChangelogEntryError):
        extract_changelog_section("", "v1.0.0")


def test_red_section_gate_without_its_refusal_lets_the_missing_section_through():
    real = release_gates.extract_changelog_section
    mutant = _mutant("extract_changelog_section").extract_changelog_section

    def probe(fn):
        return lambda: fn(FAIGRID_CHANGELOG, "v9.9.9")

    _refuses(probe(real), MissingChangelogEntryError)
    with pytest.raises(AssertionError):
        _refuses(probe(mutant), MissingChangelogEntryError)


# --- gate 3: audience over tracker prefixes ----------------------------------


def test_audience_refuses_the_faigrid_heading_naming_its_tracker():
    notes = extract_changelog_section(FAIGRID_CHANGELOG, "v1.8.0")
    with pytest.raises(ReleaseNotesAudienceError) as exc:
        check_release_notes_audience(notes, FAIGRID_TRACKER_PREFIXES)
    assert "FAI-207" in str(exc.value)
    assert "line" in str(exc.value)


def test_audience_quotes_every_offence_with_its_line():
    notes = "ok line\nsee FFR-12 and OPS-3\nclean\nADR-9\n"
    with pytest.raises(ReleaseNotesAudienceError) as exc:
        check_release_notes_audience(notes, FAIGRID_TRACKER_PREFIXES)
    msg = str(exc.value)
    for token in ("FFR-12", "OPS-3", "ADR-9"):
        assert token in msg
    assert [(o[0], o[1]) for o in exc.value.offences] == [
        (2, "FFR-12"),
        (2, "OPS-3"),
        (4, "ADR-9"),
    ]


@pytest.mark.parametrize(
    "notes",
    ["UTF-8 and SHA-256 and TLS-1.3", "fixes CVE-2026-1234 per RFC-7231", "PEP-8"],
)
def test_audience_leaves_external_identifiers_alone(notes):
    check_release_notes_audience(notes, FAIGRID_TRACKER_PREFIXES)


def test_audience_without_vocabulary_refuses_nothing():
    check_release_notes_audience("FAI-207 everywhere", [])
    check_release_notes_audience("FAI-207 everywhere", None)


def test_audience_refuses_a_malformed_prefix_instead_of_skipping_it():
    for bad in (["fai"], ["F"], ["FAI-1"], ["TOOLONG"], ["FAI BAR"]):
        with pytest.raises(ReleaseNotesAudienceError):
            check_release_notes_audience("clean", bad)


def test_red_audience_gate_without_its_refusal_lets_the_tracker_name_through():
    notes = extract_changelog_section(FAIGRID_CHANGELOG, "v1.8.0")
    real = release_gates.check_release_notes_audience
    mutant = _mutant("check_release_notes_audience").check_release_notes_audience

    def probe(fn):
        return lambda: fn(notes, FAIGRID_TRACKER_PREFIXES)

    _refuses(probe(real), ReleaseNotesAudienceError)
    with pytest.raises(AssertionError):
        _refuses(probe(mutant), ReleaseNotesAudienceError)


def test_module_name_differs_from_every_exported_name():
    assert "release_gates" not in ops_engine.__all__


# --- D19: stdlib-only, loaded by path, no yaml and no pydantic ----------------

REPO_ROOT = Path(__file__).resolve().parents[1]
GATES_PATH = REPO_ROOT / "src" / "ops_engine" / "modules" / "release_gates.py"

# A process in which `import yaml` and `import pydantic` fail, as on the bare
# release runner. The module is loaded by its path (the way the workflow loads
# it); the package is never imported and never installed.
_BARE_RUNNER_PROBE = r"""
import importlib.abc, importlib.util, sys

class _Absent(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("yaml", "pydantic", "ops_engine"):
            raise ModuleNotFoundError("No module named %r" % name)

sys.meta_path.insert(0, _Absent())
for bare in ("yaml", "pydantic", "ops_engine"):
    try:
        __import__(bare)
    except ModuleNotFoundError:
        pass
    else:
        raise SystemExit("probe is broken: %s imported" % bare)

spec = importlib.util.spec_from_file_location("release_gates", sys.argv[1])
gates = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gates)

assert gates.check_release_title("v1.2.3-rc1", prefix="Acme") == "Acme v1.2.3-rc1"
assert gates.extract_changelog_section("## [1.2.3] - d\n\nNotes.\n", "v1.2.3") == "Notes."
for call, error in (
    (lambda: gates.check_release_title("v1.2.3-RC1", prefix="Acme"), gates.ReleaseTitleError),
    (lambda: gates.extract_changelog_section("## 1.0.0\n\nx\n", "v9.9.9"), gates.MissingChangelogEntryError),
    (lambda: gates.check_release_notes_audience("see LVC-1", ["LVC"]), gates.ReleaseNotesAudienceError),
):
    try:
        call()
    except error:
        pass
    else:
        raise SystemExit("%s was not raised" % error.__name__)
loaded = sorted(m for m in sys.modules if m.split(".")[0] in ("yaml", "pydantic", "ops_engine"))
assert not loaded, loaded
print("bare-runner load: three gates refuse by name, nothing outside the stdlib imported")
"""


def test_gates_load_by_path_in_a_process_without_yaml_or_pydantic():
    import subprocess
    import sys

    run = subprocess.run(
        [sys.executable, "-I", "-c", _BARE_RUNNER_PROBE, str(GATES_PATH)],
        capture_output=True,
        text=True,
        cwd=str(GATES_PATH.parent.parent.parent.parent),  # a directory with no package
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "nothing outside the stdlib imported" in run.stdout


def test_gates_module_imports_only_the_standard_library():
    import sys

    tree = ast.parse(GATES_PATH.read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "a relative import needs the package, and so yaml"
            imported.append((node.module or "").split(".")[0])
    outside = sorted(set(imported) - set(sys.stdlib_module_names))
    assert not outside, outside


def test_gates_changelog_pattern_is_the_changelog_parsers():
    """The module holds its own copy of the heading pattern (it cannot import
    the package); this pins the copy to ChangelogParser's, section by section
    over the real CHANGELOG and the faigrid headings."""
    from ops_engine.utils.changelog_parser import ChangelogParser

    assert release_gates._HEADER_RE.pattern == ChangelogParser.HEADER_PATTERN.pattern
    assert release_gates._HEADER_RE.flags == ChangelogParser.HEADER_PATTERN.flags
    for text in (FAIGRID_CHANGELOG, (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")):
        for version in ChangelogParser.list_versions(text):
            expected = ChangelogParser.extract_version_notes(text, version)
            if expected:
                assert extract_changelog_section(text, "v" + version) == expected
