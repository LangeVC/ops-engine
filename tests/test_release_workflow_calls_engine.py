"""ADP-004: the release workflow calls the engine instead of reimplementing it.

The four curl steps that used to POST each forge's release API are gone; the
workflow now builds the four assets, runs the five gates, and hands publication
to ``ReleaseHandler.publish_release``. The three acceptance criteria:

1. No forge repository or API host appears as a literal in ``.forgejo/``; the
   destination reaches the engine from config.
2. The five preserved behaviours still fail the release when violated (title
   gate, changelog notes, audience gate, OSV gate, reproducibility) — each of
   these is a separate gate whose tests live in the other release test files;
   this file re-asserts the ordering and the engine-call seam.
3. A real tag push publishes to both forges — that is an operator decision and
   lives outside the test suite (a real release, not a fake run).

This file proves the SEAM the workflow now depends on: that the publish block
the workflow runs is exactly the engine call, that the destination is resolved
from config (not a literal), and that the literal-laden publication path is
gone.
"""

import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import yaml

from ops_engine.config_loader import OpsEngineConfig
from ops_engine.modules.mirror import resolve_destinations
from ops_engine.modules.release import render_release_name

REPO_ROOT = Path(__file__).resolve().parents[1]
FORGEJO = REPO_ROOT / ".forgejo"
FORGEJO_RELEASE = FORGEJO / "workflows" / "forgejo-release.yml"

WORKFLOW_CONTENT = FORGEJO_RELEASE.read_text(encoding="utf-8")


def _publish_block() -> str:
    """The engine-call Python the workflow runs, de-indented (PUBLISH_PY block)."""
    lines = WORKFLOW_CONTENT.splitlines()
    start = end = None
    for i, line in enumerate(lines):
        if "<<'PUBLISH_PY'" in line:
            start = i
        if start is not None and line.strip() == "PUBLISH_PY" and i > start:
            end = i
            break
    assert start is not None and end is not None, "PUBLISH_PY block not found"
    return textwrap.dedent("\n".join(lines[start + 1 : end]) + "\n")


# --- Criterion 1 prompt-face: the destination comes from config, not a literal


def _forge_destination_literals() -> list[str]:
    """Tokens that would name a forge repository or API host as a literal."""
    return [
        "api.github.com",
        "uploads.github.com",
        "LangeVC/ops-engine",
        "GH_REPO=",
        "GH_API=",
    ]


def test_no_forge_destination_literal_in_the_release_workflow():
    """None of the destination / API-host literals the old release workflow
    hardcoded survive in ``.forgejo/workflows/forgejo-release.yml`` — the
    release path's destination now reaches the engine from config.

    (mirror.yml's git-ref push URL is the git mirror, a separate surface owned
    by the mirror-destination lane; the release destination is what ADP-004
    moves into config.)
    """
    offending: list[int] = []
    for lit in _forge_destination_literals():
        for lineno, line in enumerate(WORKFLOW_CONTENT.splitlines(), start=1):
            if lit in line:
                offending.append(lineno)
    assert not offending, f"destination literals still present at lines: {offending}"


def test_publish_block_resolves_destinations_from_config():
    """The publish block reads the committed .ops.yaml via load_ops_yaml and
    materialises a typed OpsEngineConfig from it; no Actions variable and no
    destination literal survive in the workflow layer (ADP-008)."""
    block = _publish_block()
    assert "load_ops_yaml" in block
    assert "RELEASE_DESTINATIONS" not in block
    assert "OpsEngineConfig.load" in block
    assert "github.repository" in WORKFLOW_CONTENT or "FORGEJO_REPOSITORY" in block


def test_publish_block_calls_the_engine():
    """The publish block invokes ReleaseHandler.publish_release — it does not
    emit any curl/HTTP construction of its own."""
    block = _publish_block()
    assert "ReleaseHandler.publish_release" in block
    # No hand-rolled release HTTP: the block constructs adapters and delegates.
    assert "curl" not in block
    assert "httpx" not in block


def test_name_template_renders_the_gated_convention():
    """The workflow's name_template renders the same name the title gate
    validated: 'Ops Engine {tag_name}' against the tag yields exactly the
    gated convention 'Ops Engine v3.2.0'."""
    assert "Ops Engine {tag_name}" in WORKFLOW_CONTENT
    name = render_release_name("Ops Engine {tag_name}", "langevc/ops-engine", "v3.2.0")
    assert name == "Ops Engine v3.2.0"


# --- Criterion 2 seam: the gates still run before the engine publishes -------


def test_osv_gate_runs_before_the_engine_call():
    """The OSV gate still precedes the release-creation step, so a failing scan
    aborts before any release object exists on either forge."""
    gate_idx = WORKFLOW_CONTENT.index("Gate the SBOM against OSV")
    create_idx = WORKFLOW_CONTENT.index("name: Create Release")
    assert gate_idx < create_idx


def test_all_five_gates_precede_the_engine_call():
    """Every gate named by criterion 2 precedes the Create Release step, so a
    violation fails the run before publication."""
    gates = [
        "Gate the release title",
        "Extract release notes from CHANGELOG",
        "Gate the release notes for an external audience",
        "Build reproducible assets",
        "Gate the SBOM against OSV",
    ]
    create_idx = WORKFLOW_CONTENT.index("name: Create Release")
    for gate in gates:
        assert WORKFLOW_CONTENT.index(gate) < create_idx, gate


def test_publish_block_constructs_one_adapter_per_destination():
    """The publish block maps one credential per forge through the factory and
    delegates adapter construction to it, rather than hand-building a
    ForgejoAdapter / GithubAdapter inline — two forges, two adapters, two
    distinct tokens, no shared credential (ADP-011)."""
    block = _publish_block()
    assert "adapters_for" in block
    assert "Credential" in block
    # No inline adapter construction: the factory owns the mapping.
    assert "ForgejoAdapter" not in block
    assert "GithubAdapter" not in block
    # The two credentials are distinct tokens, not one shared one.
    assert "FORGEJO_TOKEN" in block
    assert "GH_MIRROR_TOKEN" in block


# --- The destination-to-adapter wiring actually runs (real execution) ---------


def test_config_from_the_workflow_shape_resolves_two_destinations():
    """Reconstruct the config the workflow builds and prove the engine resolves
    exactly the canonical Forgejo destination plus the configured mirrors — a
    real run of resolve_destinations, not a text read."""
    raw_destinations = yaml.safe_load(
        "- {forge: github, repo: LangeVC/ops-engine, role: release}\n"
    )
    repo = "langevc/ops-engine"
    org, _, repo_name = repo.partition("/")
    config = OpsEngineConfig.load(
        {
            "orgs": {
                org: {
                    "repositories": {
                        repo_name: {
                            "destinations": [
                                {"forge": "forgejo", "repo": repo, "role": "release"}
                            ]
                            + raw_destinations
                        }
                    }
                }
            }
        }
    )
    destinations = resolve_destinations(config, repo)
    assert [(d.forge, d.repo) for d in destinations] == [
        ("forgejo", "langevc/ops-engine"),
        ("github", "LangeVC/ops-engine"),
    ]


# --- REL-020: the mirror tag is pushed from the canonical tag object, not ----
# --- invented by the release API. -------------------------------------------


def _create_release_run_block() -> str:
    """The Create Release step's ``run`` block, rendered from the YAML exactly as
    the runner decodes it (the block scalar is the run text)."""
    wf = yaml.safe_load(FORGEJO_RELEASE.read_text(encoding="utf-8"))
    for job in wf["jobs"].values():
        for step in job["steps"]:
            if step.get("name") == "Create Release":
                return step["run"]
    raise AssertionError("Create Release step not found")


def _extract_toplevel_def(name: str) -> str:
    """Extract one top-level (``def``/``async def``) function body from the run
    block's embedded Python, de-indented and self-contained enough to run."""
    lines = _create_release_run_block().splitlines()
    start = end = None
    for i, line in enumerate(lines):
        if "<<'PUBLISH_PY'" in line:
            start = i
        if start is not None and line.strip() == "PUBLISH_PY" and i > start:
            end = i
            break
    assert start is not None and end is not None, "PUBLISH_PY block not found"
    body = textwrap.dedent("\n".join(lines[start + 1 : end]) + "\n")
    body_lines = body.splitlines()
    # locate the named def and copy through to the next top-level def.
    begin = None
    for i, line in enumerate(body_lines):
        if line.startswith(f"def {name}("):
            begin = i
            break
    assert begin is not None, f"def {name} not found in PUBLISH_PY"
    snippet = [body_lines[begin]]
    for line in body_lines[begin + 1 :]:
        if line.startswith("def ") or line.startswith("async def "):
            break
        snippet.append(line)
    return "\n".join(snippet) + "\n"


def _run_verify(tag, canonical_sha, remote_path):
    """Run the extracted ``_verify_mirror_tag`` against a LOCAL bare mirror repo,
    exercising the exact git commands the workflow runs."""
    fn = _extract_toplevel_def("_verify_mirror_tag")
    driver = (
        "import subprocess, sys\n"
        "import sys as _sys\n"
        + fn
        + f"_verify_mirror_tag({tag!r}, {canonical_sha!r}, {remote_path!r})\n"
    )
    return subprocess.run(
        [sys.executable, "-c", driver], capture_output=True, text=True
    )


def test_rel020_publishes_from_peeled_canonical_tag_object_never_the_api():
    """The tag the mirror receives is pushed from ``refs/tags/<tag>`` by real git
    (``git push --force <remote> refs/tags/<tag>:refs/tags/<tag>``, the exact
    verbatim mirror mirror.yml performs), and that push runs BEFORE the engine's
    ``publish_release`` (whose github create_release would otherwise invent the
    tag at the mirror's default-branch head). This is the mechanism, asserted on
    the rendered run block, not on a comment that claims it."""
    block = _publish_block()
    assert "refs/tags/{tag_name}:refs/tags/{tag_name}" in block
    assert '"git", "push", "--force", remote' in block
    # The push is invoked before the publish in main(); the verify after.
    publish_idx = block.index("publish_release(")
    assert block.index("_push_mirror_tag(_mirror_remote") < publish_idx
    assert block.rindex("_verify_mirror_tag(") > publish_idx


def test_rel020_mirror_tag_verify_passes_when_targets_match():
    """Green proof: the extracted verify, run against a matching local mirror tag,
    exits zero and reports the target."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        canonical = root / "canonical"
        _git_init_repo(canonical)
        commit_a = _commit(canonical, "release content")
        _git(canonical, "tag", "vtest")
        mirror = _bare_repo(root / "mirror.git")
        _git(canonical, "push", str(mirror), "refs/tags/vtest:refs/tags/vtest")
        r = _run_verify("vtest", commit_a, str(mirror))
    assert r.returncode == 0, r.stderr
    assert commit_a in r.stdout


def test_rel020_mirror_tag_verify_refuses_by_name_when_targets_differ():
    """Red proof: point the mirror tag elsewhere by hand and show the exact check
    failing with the named MirrorTagDriftError and a non-zero exit."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        canonical = root / "canonical"
        _git_init_repo(canonical)
        commit_a = _commit(canonical, "release content")
        _git(canonical, "tag", "vtest")
        mirror = _bare_repo(root / "mirror.git")
        # The mirror tag is moved ELSEWHERE by hand (as in v3.4.0).
        commit_b = _commit(canonical, "wrong, older content")
        _git(canonical, "push", "--force", str(mirror), f"{commit_b}:refs/tags/vtest")
        r = _run_verify("vtest", commit_a, str(mirror))
    assert r.returncode != 0, r.stdout
    assert "MirrorTagDriftError" in r.stderr
    assert commit_a in r.stderr
    assert commit_b in r.stderr


def test_rel020_run_block_is_sound_as_the_runner_renders_it():
    """Criterion 3: render the run block from the YAML, prove the shell parses
    (``bash -n``), and prove the embedded PUBLISH_PY interpreter compiles — the
    runner renders the block and runs the interpreter, so this is the seam the
    distinction from a local re-run is guarding."""
    run_block = _create_release_run_block()
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write(run_block)
        script = fh.name
    r = subprocess.run(["bash", "-n", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    # The embedded interpreter is the PUBLISH_PY block; compile it.
    body = _publish_block()
    src = tempfile.NamedTemporaryFile(suffix=".py", delete=False)
    src.write(body.encode("utf-8"))
    src.close()
    import py_compile

    py_compile.compile(src.name, doraise=True)
    Path(src.name).unlink()
    Path(script).unlink()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def _git_init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@t"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "t"], check=True
    )


def _commit(repo: Path, content: str) -> str:
    (repo / "f.txt").write_text(content, encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", content)
    return _git(repo, "rev-parse", "HEAD").strip()


def _bare_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
    return path
