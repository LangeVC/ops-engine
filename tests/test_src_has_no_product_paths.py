"""OE-037 (D14): the template's source carries no product planning paths.

The path classes that may carry planning substrate are the adopter's
configuration (``visibility_candidate_globs``). No module under ``src`` may
declare them, and the former default must not come back.
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

PRODUCT_PATH_LITERALS = (".skillweave", "prd*.md", "prd*.json", "strategy.md")


def _sources():
    return sorted(SRC.rglob("*.py"))


def test_default_candidate_globs_constant_is_gone():
    offenders = [
        str(p.relative_to(SRC))
        for p in _sources()
        if "DEFAULT_VISIBILITY_CANDIDATE_GLOBS" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_no_module_declares_a_product_planning_path():
    offenders = [
        f"{p.relative_to(SRC)}: {literal}"
        for p in _sources()
        for literal in PRODUCT_PATH_LITERALS
        if literal in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


# A product's directory, file or convention name, case-insensitive. Dotted
# directories, the dot-less product name used as a path segment, and the PRD
# document file names. A token is matched only where it starts a path segment,
# so unrelated words do not hit.
PRODUCT_PATH_PATTERNS = (
    r"(?<![\w-])\.skillweave\b",
    r"(?<![\w-])skillweave/",
    r"(?<![\w-])\.planning\b",
    r"(?<![\w-])\.claude\b",
    r"(?<![\w-])\.oes-state\b",
    r"(?<![\w-])planning/prds\b",
    r"(?<![\w-])prds/[\w.-]+/sequences\b",
    r"(?<![\w-])prd\.(?:schema\.)?json\b",
    r"(?<![\w-])[\w-]*\.prd\.[\w.]+",
)


def find_product_paths(root: Path = SRC) -> list[str]:
    pattern = re.compile("|".join(PRODUCT_PATH_PATTERNS), re.IGNORECASE)
    hits = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in {".py", ".yml", ".yaml", ".json", ".md", ".sh", ".txt"}:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            match = pattern.search(line)
            if match:
                hits.append(f"{path.relative_to(root.parent)}:{number}: {match.group(0)}")
    return hits


def test_src_names_no_product_path_or_convention():
    hits = find_product_paths()
    assert not hits, "src names a product path or convention:\n" + "\n".join(hits)


import pytest  # noqa: E402


@pytest.mark.parametrize(
    "planted",
    [".skillweave/", ".SkillWeave/", "skillweave/", "prd.schema.json", "prd.json", "x.prd.md"],
)
def test_the_guard_detects_a_planted_product_path(tmp_path, planted):
    module = tmp_path / "ops_engine" / "planted.py"
    module.parent.mkdir()
    module.write_text(f'PATH = "{planted}"\n', encoding="utf-8")
    assert find_product_paths(tmp_path / "ops_engine"), f"the guard missed {planted}"
