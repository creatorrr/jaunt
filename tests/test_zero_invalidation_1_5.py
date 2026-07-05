"""Release gate: jaunt 1.5 introduces zero mass invalidation on upgrade.

Spec "Upgrade & compatibility requirements": a freshly built project reports
zero stale modules and `jaunt check` exits 0, and neither `status` nor `check`
rewrites the generated provenance headers. Combined with the fingerprint carve-out
guards (see ``test_advisories_parse.py`` and below), this pins the invariant that
none of the 1.5 changes (advisories prompt block, skills frontmatter fix,
context_stats relabel) can restale already-built modules.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import jaunt.cli
from test_cli_summaries import _make_cli_build_project
from test_regressions_review_fixes import GoodBackend, _restore_modules


def _run(project: Path, prefix: str, argv: list[str], *, backend=None) -> int:
    """Run a CLI command with module isolation; install ``backend`` for build."""
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs": sys.modules.get(f"{prefix}.specs"),
    }
    orig_sys_path = list(sys.path)
    orig_backend = jaunt.cli._build_backend
    if backend is not None:
        jaunt.cli._build_backend = lambda cfg: backend  # type: ignore[assignment]
    try:
        rc = jaunt.cli.main(argv)
    finally:
        jaunt.cli._build_backend = orig_backend
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)
    return rc


def _snapshot_generated(project: Path) -> dict[str, str]:
    """Map relpath -> file bytes for every generated .py/.pyi under the project."""
    snap: dict[str, str] = {}
    for path in sorted(project.rglob("*")):
        if path.is_file() and ("__generated__" in path.parts or path.suffix == ".pyi"):
            snap[str(path.relative_to(project))] = path.read_text(encoding="utf-8")
    return snap


def test_fresh_build_is_zero_stale_and_check_exits_ok(tmp_path, capsys) -> None:
    project, prefix = _make_cli_build_project(tmp_path)

    rc = _run(project, prefix, ["build", "--root", str(project), "--json"], backend=GoodBackend())
    payload = json.loads(capsys.readouterr().out)
    assert rc == jaunt.cli.EXIT_OK
    assert payload["generated"] == ["app.specs"]

    before = _snapshot_generated(project)
    assert before, "build must have emitted generated artifacts"

    # status: zero stale (this is the upgrade invariant — nothing restales)
    rc = _run(project, prefix, ["status", "--root", str(project), "--json"])
    status_payload = json.loads(capsys.readouterr().out)
    assert rc == jaunt.cli.EXIT_OK
    assert status_payload["stale"] == []
    assert status_payload["orphans"] == []

    # check (magic-only): the deterministic CI gate exits 0
    rc = _run(project, prefix, ["check", "--magic-only", "--root", str(project)])
    capsys.readouterr()
    assert rc == jaunt.cli.EXIT_OK

    # neither status nor check may rewrite provenance headers
    after = _snapshot_generated(project)
    assert after == before


def test_advisories_instruction_absent_from_fingerprinted_templates() -> None:
    # Zero-invalidation guard (spec §2 fingerprint carve-out): the advisories
    # instruction must never live in the prompt templates, which participate in
    # the generation fingerprint.
    prompts = Path("src/jaunt/prompts")
    for template in prompts.glob("*.md"):
        assert "ADVISORIES" not in template.read_text(encoding="utf-8"), template


def test_stub_format_version_not_bumped_by_1_5() -> None:
    # Spec "Upgrade & compatibility" #2: no emitter format changes in 1.5.
    from jaunt import stub_emitter

    assert stub_emitter._STUB_FORMAT_VERSION == "2"
