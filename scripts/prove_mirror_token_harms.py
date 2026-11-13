#!/usr/bin/env python3
"""prove_mirror_token_harms - show the two P36-002 harms against a checkout.

The two RED-proof tests for P36-002 (``test_token_in_url_appears_in_git_process_argv``
and ``test_missing_mirror_secret_aborts_with_keyerror_on_base``) fail on the
candidate BY DESIGN: they assert the undesirable 3.5.2 behaviour so that the
inversion is visible, not a green suite. A collected test that has to fail at
the tip is a false signal for every later run, so both proofs live here instead.

Contract: this script exits 0 ONLY when the harm is shown against the checkout
given by ``--root``. Against the candidate tip it exits 1 (the harm is gone).

The two harms:

  1. ``argv`` - ``_run_git`` spawned with a token-embedded mirror URL leaves
     the token in the git subprocess argument list. Shown by importing
     ``_run_git`` from ``<root>/src`` in a child interpreter and capturing the
     argv passed to ``asyncio.create_subprocess_exec``.

  2. ``missing-secret`` - the shipped release workflow's mirror credential
     panel aborts with an uncaught ``KeyError`` when ``GH_MIRROR_TOKEN`` is
     absent, so the Create Release step dies before ``publish_release`` and
     before the other destination is attempted. Shown by extracting the
     ``PUBLISH_PY`` block from ``<root>/.forgejo/workflows/forgejo-release.yml``
     and driving its mirror-remote/credential helpers without the variable.

Usage:
    prove_mirror_token_harms.py --root PATH [--harm argv|missing-secret|all]

Exit codes:
    0  every requested harm is shown (the checkout carries the 3.5.2 behaviour)
    1  at least one requested harm is not shown (the candidate fixed it)
    2  usage, extraction or environment error
"""

import argparse
import ast
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

_HARM_CHOICES = ("argv", "missing-secret", "all")

_ARGV_DRIVER = r"""
import asyncio
import json
from unittest.mock import patch

from ops_engine.modules.mirror import _run_git


class _Proc:
    returncode = 0

    async def communicate(self):
        return (b"", b"")


TOKEN = "ghp_proofToken123abc"
captured = []


async def _fake_exec(*argv, **kwargs):
    captured.append([str(a) for a in argv])
    return _Proc()


async def _main():
    url = f"http://x-access-token:{TOKEN}@127.0.0.1:19999/org/repo.git"
    with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
        try:
            await _run_git(
                ["git", "push", url, "HEAD:refs/heads/main"], "."
            )
        except Exception:
            pass
    flat = " ".join(" ".join(argv) for argv in captured)
    print(json.dumps({
        "spawned": len(captured),
        "token_in_argv": TOKEN in flat,
        "argv": captured,
    }))
    return 0 if TOKEN in flat else 1


raise SystemExit(asyncio.run(_main()))
"""

_MISSING_SECRET_DRIVER = r"""
import json
import sys

mirror_dest = _FakeDestination("exampleorg/engine")
result = {"remote_built": False, "credential_call": None, "error": None}
try:
    remote = _mirror_remote(mirror_dest.repo)
    result["remote_built"] = True
    result["remote_has_token"] = "x-access-token" in remote
    if "_mirror_credential_env" in globals():
        result["credential_call"] = "present"
        _mirror_credential_env()
        result["error"] = "no error"
except KeyError as exc:
    result["error"] = f"KeyError: {exc}"
except Exception as exc:  # a named, catchable error is the fixed behaviour
    result["error"] = f"{type(exc).__name__}: {exc}"
print(json.dumps(result))
raise SystemExit(0 if result["error"].startswith("KeyError") else 1)
"""


def _publish_py_block(workflow_path: Path) -> str:
    """Return the dedented Python of the PUBLISH_PY heredoc."""
    lines = workflow_path.read_text(encoding="utf-8").splitlines()
    start = None
    for i, line in enumerate(lines):
        if "<<'PUBLISH_PY'" in line:
            start = i + 1
            break
    if start is None:
        raise RuntimeError(f"no PUBLISH_PY heredoc in {workflow_path}")
    end = None
    for j in range(start, len(lines)):
        if lines[j].strip() == "PUBLISH_PY":
            end = j
            break
    if end is None:
        raise RuntimeError(f"unterminated PUBLISH_PY heredoc in {workflow_path}")
    return textwrap.dedent("\n".join(lines[start:end])) + "\n"


def _mirror_helpers_source(block: str) -> str:
    """Return the module-level imports plus the mirror helper definitions.

    Only the pieces the missing-secret check needs: every module-level import,
    the ``MirrorCredentialError`` class when present, and the functions
    ``_mirror_remote`` / ``_mirror_credential_env``. Nothing that reads the
    Layer-3 config or performs network I/O is included.
    """
    tree = ast.parse(block)
    wanted = {"_mirror_remote", "_mirror_credential_env"}
    keep = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            keep.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "MirrorCredentialError":
            keep.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in wanted:
            keep.append(node)
    names = {
        node.name
        for node in keep
        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
    }
    missing = wanted - names
    if "_mirror_remote" in missing:
        raise RuntimeError("_mirror_remote not found in PUBLISH_PY")
    # ``_mirror_credential_env`` is the candidate's helper; its absence IS the
    # 3.5.2 behaviour the missing-secret harm rests on.
    module = ast.Module(body=keep, type_ignores=[])
    ast.fix_missing_locations(module)
    return ast.unparse(module) + "\n"


def _check_argv(root: Path) -> bool:
    """True when a token-embedded push URL reaches the git argv."""
    env = {**os.environ, "PYTHONPATH": str(root / "src")}
    completed = subprocess.run(
        [sys.executable, "-c", _ARGV_DRIVER],
        capture_output=True,
        text=True,
        env=env,
    )
    sys.stdout.write(completed.stdout)
    if completed.stderr.strip():
        sys.stderr.write(completed.stderr)
    return completed.returncode == 0


def _check_missing_secret(root: Path) -> bool:
    """True when an absent GH_MIRROR_TOKEN aborts the mirror helpers uncaught."""
    workflow = root / ".forgejo" / "workflows" / "forgejo-release.yml"
    block = _publish_py_block(workflow)
    helpers = _mirror_helpers_source(block)
    driver = (
        helpers
        + "\n"
        + "class _FakeDestination:\n"
        + "    def __init__(self, repo):\n"
        + "        self.repo = repo\n\n"
        + _MISSING_SECRET_DRIVER
    )
    with tempfile.TemporaryDirectory() as tmp:
        driver_path = Path(tmp) / "missing_secret_driver.py"
        driver_path.write_text(driver, encoding="utf-8")
        env = {
            k: v for k, v in os.environ.items() if k != "GH_MIRROR_TOKEN"
        }
        env["PYTHONPATH"] = str(root / "src")
        completed = subprocess.run(
            [sys.executable, str(driver_path)],
            capture_output=True,
            text=True,
            env=env,
        )
    sys.stdout.write(completed.stdout)
    if completed.stderr.strip():
        sys.stderr.write(completed.stderr)
    return completed.returncode == 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Exit 0 only when the P36-002 harms are shown in --root.",
    )
    parser.add_argument(
        "--root",
        required=True,
        help="checkout to inspect (uses <root>/src and <root>/.forgejo)",
    )
    parser.add_argument(
        "--harm",
        choices=_HARM_CHOICES,
        default="all",
        help="which harm to demonstrate (default: all)",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not (root / "src" / "ops_engine").is_dir():
        print(f"error: {root} has no src/ops_engine", file=sys.stderr)
        return 2
    if not (root / ".forgejo" / "workflows" / "forgejo-release.yml").is_file():
        print(f"error: {root} has no forgejo-release workflow", file=sys.stderr)
        return 2

    checks = {
        "argv": _check_argv,
        "missing-secret": _check_missing_secret,
    }
    selected = list(checks) if args.harm == "all" else [args.harm]

    all_shown = True
    for name in selected:
        print(f"== harm: {name} (root: {root})", file=sys.stderr)
        try:
            shown = checks[name](root)
        except Exception as exc:  # extraction / import failure is not a verdict
            print(f"error: {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        print(
            f"{name}: {'HARM SHOWN' if shown else 'NOT SHOWN (fixed)'}",
            file=sys.stderr,
        )
        all_shown = all_shown and shown

    return 0 if all_shown else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
