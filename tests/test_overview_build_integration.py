"""Integration test: project overview block is injected into build prompts."""

from __future__ import annotations

import sys
from pathlib import Path

import jaunt.cli
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext


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


class _OverviewCapturingBackend(GeneratorBackend):
    """Backend that captures the full prompt from _build_prompt and returns valid stubs."""

    def __init__(self) -> None:
        self.captured_prompts: list[str] = []

    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> tuple[str, None]:
        # Build the prompt the same way CodexBackend would so we can inspect it.
        # We reach into the codex backend's _build_prompt via the context.
        # Since we are the backend and we receive ctx, we store the overview block.
        self.captured_prompts.append(getattr(ctx, "project_overview_block", "") or "")
        lines: list[str] = []
        for name in ctx.expected_names:
            lines.append(f"def {name}() -> None:\n    assert True\n")
        return "\n".join(lines).rstrip() + "\n", None

    async def complete_text(self, *, system: str, user: str) -> str:
        return "OVERVIEW PROSE"


def test_overview_block_propagated_to_ctx_when_enabled(tmp_path: Path, monkeypatch) -> None:
    """When context.overview = true, project_overview_block arrives on each ModuleSpecContext."""
    project, prefix = _make_cli_build_project_with_overview(tmp_path)
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs": sys.modules.get(f"{prefix}.specs"),
    }
    orig_sys_path = list(sys.path)

    backend = _OverviewCapturingBackend()
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: backend)

    try:
        rc = jaunt.cli.main(["build", "--root", str(project)])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    assert rc == jaunt.cli.EXIT_OK
    assert backend.captured_prompts, "generate_module was never called — no prompts captured"
    assert all("OVERVIEW PROSE" in p for p in backend.captured_prompts), (
        f"Expected 'OVERVIEW PROSE' in project_overview_block, got: {backend.captured_prompts!r}"
    )


def test_overview_block_absent_when_disabled(tmp_path: Path, monkeypatch) -> None:
    """When context.overview = false (default), project_overview_block is empty."""
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

    backend = _OverviewCapturingBackend()
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: backend)

    try:
        rc = jaunt.cli.main(["build", "--root", str(project)])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    assert rc == jaunt.cli.EXIT_OK
    assert backend.captured_prompts, "generate_module was never called — no prompts captured"
    # complete_text should NOT have been called, and overview block should be empty
    assert all(p == "" for p in backend.captured_prompts), (
        f"Expected empty project_overview_block when disabled, got: {backend.captured_prompts!r}"
    )
