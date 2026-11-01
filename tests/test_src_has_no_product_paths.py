"""OE-037 (D14): the template's source carries no product planning paths.

The path classes that may carry planning substrate are the adopter's
configuration (``visibility_candidate_globs``). No module under ``src`` may
declare them, and the former default must not come back.
"""

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
