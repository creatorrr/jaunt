"""Integration test: project overview block is injected into build prompts."""

from __future__ import annotations

import sys
from pathlib import Path

import jaunt.cli
from jaunt.generate.codex_backend import CodexBackend
from jaunt.generate.base import ModuleSpecContext


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_package_init(root: Path, rel_dir: str) -> None:
    cur = root
    for part in Path(rel_dir).parts:
        cur = cur / part
        cur.mkdir(parents=True, exist_ok=True)
        (cur / "__init__.py").write_text("", encoding="utf-8")


def _restore_modules(prefixes: list[str], *, before: dict[str, object | None]) -> None:
    for name in list(sys.modules):
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes):
            sys.modules.pop(name, None)
    for name, module in before.items():
        if module is not None:
            sys.modules[name] = module  # type: ignore[assignment]


def _make_cli_build_project_with_overview(root: Path) -> tuple[Path, str]:
    """Create a minimal jaunt project with context.overview = true."""
    project = root / "proj"
    project.mkdir(parents=True, exist_ok=True)
    _write(
        project / "jaunt.toml",
        "\n".join(
            [
                "version = 1",
                "",
                "[paths]",
                'source_roots = ["src"]',
                'test_roots = ["tests"]',
                'generated_dir = "__generated__"',
                "",
                "[context]",
                "overview = true",
                "",
            ]
        ),
    )
    _write_package_init(project, "src/app")
    _write(
        project / "src" / "app" / "specs.py",
        "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import jaunt",
                "",
                "@jaunt.magic()",
                "def generated_smoke() -> None:",
                '    """Generate a no-op smoke function."""',
                '    raise RuntimeError("spec stub")',
                "",
            ]
        ),
    )
    return project, "app"


def test_overview_block_appears_in_rendered_prompt(tmp_path: Path, monkeypatch) -> None:
    """When context.overview = true, 'OVERVIEW PROSE' appears in the rendered _build_prompt.

    Uses the real CodexBackend so that _build_prompt assembles the prompt exactly as
    production does. Only complete_text (returns the overview prose) and generate_module
    (captures the rendered prompt, returns valid stubs) are monkeypatched.
    """
    project, prefix = _make_cli_build_project_with_overview(tmp_path)
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs": sys.modules.get(f"{prefix}.specs"),
    }
    orig_sys_path = list(sys.path)

    captured: dict[str, str] = {}

    async def _fake_complete_text(self: CodexBackend, *, system: str, user: str) -> str:
        return "OVERVIEW PROSE"

    async def _fake_generate_module(
        self: CodexBackend,
        ctx: ModuleSpecContext,
        *,
        extra_error_context: list[str] | None = None,
    ) -> tuple[str, None]:
        # Render the full prompt exactly as the real backend would.
        captured["prompt"] = self._build_prompt(ctx, Path("x.py"), None)
        lines = [f"def {name}() -> None:\n    assert True\n" for name in ctx.expected_names]
        return "\n".join(lines).rstrip() + "\n", None

    monkeypatch.setattr(CodexBackend, "complete_text", _fake_complete_text)
    monkeypatch.setattr(CodexBackend, "generate_module", _fake_generate_module)

    try:
        rc = jaunt.cli.main(["build", "--root", str(project)])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    assert rc == jaunt.cli.EXIT_OK
    assert "prompt" in captured, "generate_module was never called — no prompt captured"
    assert "OVERVIEW PROSE" in captured["prompt"], (
        f"Expected 'OVERVIEW PROSE' in rendered prompt, got:\n{captured['prompt'][:500]}"
    )


def test_overview_block_absent_when_disabled(tmp_path: Path, monkeypatch) -> None:
    """When context.overview = false (default), complete_text is never called."""
    project = tmp_path / "proj2"
    project.mkdir(parents=True, exist_ok=True)
    _write(
        project / "jaunt.toml",
        "\n".join(
            [
                "version = 1",
                "",
                "[paths]",
                'source_roots = ["src"]',
                'test_roots = ["tests"]',
                'generated_dir = "__generated__"',
                "",
                # No [context] section → overview defaults False
            ]
        ),
    )
    _write_package_init(project, "src/app2")
    _write(
        project / "src" / "app2" / "specs.py",
        "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import jaunt",
                "",
                "@jaunt.magic()",
                "def smoke2() -> None:",
                '    """A smoke spec."""',
                '    raise RuntimeError("spec stub")',
                "",
            ]
        ),
    )
    prefix = "app2"
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs": sys.modules.get(f"{prefix}.specs"),
    }
    orig_sys_path = list(sys.path)

    complete_text_called = {"count": 0}
    captured: dict[str, str] = {}

    async def _fake_complete_text(self: CodexBackend, *, system: str, user: str) -> str:
        complete_text_called["count"] += 1
        return "SHOULD NOT APPEAR"

    async def _fake_generate_module(
        self: CodexBackend,
        ctx: ModuleSpecContext,
        *,
        extra_error_context: list[str] | None = None,
    ) -> tuple[str, None]:
        captured["prompt"] = self._build_prompt(ctx, Path("x.py"), None)
        lines = [f"def {name}() -> None:\n    assert True\n" for name in ctx.expected_names]
        return "\n".join(lines).rstrip() + "\n", None

    monkeypatch.setattr(CodexBackend, "complete_text", _fake_complete_text)
    monkeypatch.setattr(CodexBackend, "generate_module", _fake_generate_module)

    try:
        rc = jaunt.cli.main(["build", "--root", str(project)])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    assert rc == jaunt.cli.EXIT_OK
    assert "prompt" in captured, "generate_module was never called — no prompt captured"
    assert complete_text_called["count"] == 0, (
        "complete_text should NOT be called when overview is disabled"
    )
    assert "SHOULD NOT APPEAR" not in captured["prompt"], (
        f"Overview prose leaked into prompt when disabled:\n{captured['prompt'][:500]}"
    )
