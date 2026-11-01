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


# Structural guard: tokens, not an enumeration of file names. Case-insensitive,
# applied to every path segment AND to the content of every file under src,
# whatever its extension (a file that does not decode as text is skipped).
# A token starts a path segment, so unrelated words do not hit; the bare prose
# word "skillweave" is not a token.
PRODUCT_PATH_PATTERNS = (
    r"(?<![\w-])\.skillweave",                    # the product directory
    r"(?<![\w-])skillweave[\w-]*/",               # skillweave/, skillweave-blueprint/
    r"(?<![\w-])skillweave[-_][\w-]*",            # skillweave-x, skillweave_blueprint
    r"(?<![\w-])\.planning\b",
    r"(?<![\w-])\.claude\b",
    r"(?<![\w-])\.oes-state\b",
    r"(?<![\w-])planning/prds\b",
    r"(?<![\w-])prds/[\w.-]+/sequences\b",
    r"(?<![\w-])prd[\w.*-]*\.(?:md|json|ya?ml|txt)\b",   # prd.json, PRD.md, prd-template.md, prd_v2.json
    r"(?<![\w-])[\w-]*\.prd\.[\w.]+",
    r"(?<![\w-])strategy\.md\b",
)
_PATTERN = re.compile("|".join(PRODUCT_PATH_PATTERNS), re.IGNORECASE)


def find_product_paths(root: Path = SRC) -> list[str]:
    hits = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root.parent)
        name_match = _PATTERN.search("/".join(path.relative_to(root).parts))
        if name_match:
            hits.append(f"{rel}: path: {name_match.group(0)}")
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            match = _PATTERN.search(line)
            if match:
                hits.append(f"{rel}:{number}: {match.group(0)}")
    return hits


def test_src_names_no_product_path_or_convention():
    hits = find_product_paths()
    assert not hits, "src names a product path or convention:\n" + "\n".join(hits)


import pytest  # noqa: E402

_CONTENT_PLANTS = [
    ".skillweave/", ".SkillWeave/", "skillweave/", "skills/skillweave-blueprint/references/prd-template.md",
    "skillweave-blueprint/assets", "skillweave_blueprint", "prd.schema.json", "prd.json", "x.prd.md",
    "prd.md", "PRD.md", "prd-template.md", "prd_v2.json", "prd-2.json", "Strategy.md", "strategy.md",
]


@pytest.mark.parametrize("planted", _CONTENT_PLANTS)
def test_the_guard_detects_a_planted_product_path(tmp_path, planted):
    module = tmp_path / "ops_engine" / "planted.py"
    module.parent.mkdir()
    module.write_text(f'PATH = "{planted}"\n', encoding="utf-8")
    assert find_product_paths(tmp_path / "ops_engine"), f"the guard missed {planted}"


@pytest.mark.parametrize(
    "relative, content",
    [
        ("skillweave-blueprint/mod.py", "X = 1\n"),   # path segment
        ("prd.json", "{}\n"),                          # file name
        ("mod.py", "# see .planning/x\n"),             # content
        ("data.cfg", "dir = .skillweave/x\n"),         # odd extension
        ("notes.weird", "see prd-template.md\n"),      # odd extension, file name token
    ],
)
def test_the_guard_fails_a_tree_with_one_planted_token_per_class(tmp_path, relative, content):
    root = tmp_path / "ops_engine"
    (root / "ok.py").parent.mkdir(parents=True)
    (root / "ok.py").write_text("VALUE = 'a skillweave comment is prose'\n", encoding="utf-8")
    assert find_product_paths(root) == [], "a clean tree must pass"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    assert find_product_paths(root), f"the guard missed a plant in {relative}"


def test_the_guard_skips_undecodable_files(tmp_path):
    root = tmp_path / "ops_engine"
    root.mkdir()
    (root / "blob.bin").write_bytes(b"\xff\xfe\x00.skillweave"[:3])
    assert find_product_paths(root) == []
