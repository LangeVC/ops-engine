#!/usr/bin/env python3
"""e2e_verdict_gate - release only with a green E2E verdict for the candidate.

REA-023. A release object may be created only for a commit the release
end-to-end harness (REA-022) has proven green. That proof is a verdict file:

    {"candidate_sha": "<40 hex>", "result": "green" | "red",
     "runs": [...], "produced_at": "<string>"}

``candidate_sha`` names the commit the verdict is about. The candidate of a
release is the commit the tag PEELS to (``git rev-parse refs/tags/<tag>^{}``),
not the tag object: an annotated tag's own SHA proves nothing about the tree
that ships.

The gate is a GATE, not advice: it is a blocking step of the release workflow,
placed before the tag/release step, and the release job declares
``needs: gate`` so a red verdict here means no release object is created. The
verdict is evidence ABOUT the candidate and is committed after it, so it never
sits in the candidate's own tree; it is read as repository content at a
repository path (``e2e-verdict.json``), never as a secret or a CI variable.

Refusals, each naming its cause (exit 1):
  - the verdict file is absent
  - the file is not valid JSON
  - the file lacks a required key, or a key has the wrong type or shape
  - the verdict's ``candidate_sha`` is not a 40-character hex commit SHA
  - the verdict's ``candidate_sha`` is not the candidate (both are named)
  - the verdict's ``result`` is not ``green``

Exit codes:
  0  a green verdict for exactly the candidate SHA
  1  refused: one of the causes above, printed to stderr
  2  usage error (e.g. a required argument is missing)

Usage:
    e2e_verdict_gate.py --verdict e2e-verdict.json --candidate-sha <40 hex>

The script is stdlib-only (REL-006: the bare runner carries neither yaml nor
pydantic): it reads one file and writes nothing.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

PROG = "e2e-verdict-gate"
_SHA = re.compile(r"^[0-9a-f]{40}$")


def _refuse(message: str) -> int:
    print(f"{PROG}: REFUSED: {message}", file=sys.stderr)
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Accept a release candidate only with a green E2E verdict "
                    "for exactly that commit.")
    parser.add_argument("--verdict", required=True,
                        help="path to the verdict JSON (repository content, "
                             "e.g. e2e-verdict.json)")
    parser.add_argument("--candidate-sha", required=True,
                        help="the peeled commit SHA of the candidate tag")
    args = parser.parse_args(argv)

    candidate = args.candidate_sha
    path = args.verdict

    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return _refuse(f"no verdict at {path!r}")

    try:
        verdict = json.loads(text)
    except json.JSONDecodeError as exc:
        return _refuse(f"{path!r} is not valid JSON: {exc}")
    if not isinstance(verdict, dict):
        return _refuse(f"{path!r} is not a JSON object")

    if "candidate_sha" not in verdict:
        return _refuse(f"{path!r} has no 'candidate_sha' key")
    verdict_sha = verdict["candidate_sha"]
    if not isinstance(verdict_sha, str) or not _SHA.match(verdict_sha):
        return _refuse(
            f"{path!r}: candidate_sha {verdict_sha!r} is not a "
            f"40-character hex commit SHA")

    if "runs" not in verdict:
        return _refuse(f"{path!r} has no 'runs' key")
    if not isinstance(verdict["runs"], list):
        return _refuse(f"{path!r}: 'runs' is not a list")

    if "produced_at" not in verdict:
        return _refuse(f"{path!r} has no 'produced_at' key")

    result = verdict.get("result")
    if result != "green":
        return _refuse(f"{path!r}: verdict result is {result!r}, not 'green'")

    if verdict_sha != candidate:
        return _refuse(
            f"{path!r} is for another SHA: verdict names {verdict_sha}, "
            f"candidate is {candidate}")

    print(f"{PROG}: OK: green verdict for candidate {candidate}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
