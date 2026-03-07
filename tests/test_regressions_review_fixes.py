from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

import jaunt.cli
from jaunt.deps import build_spec_graph
from jaunt.discovery import evict_modules_for_import
from jaunt.errors import JauntDependencyCycleError
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext
from jaunt.registry import SpecEntry
from jaunt.spec_ref import normalize_spec_ref
from jaunt.tester import run_test_generation, run_tests
from jaunt.watcher import WatchEvent, build_cycle_runner, filter_spec_files


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


def _entry(*, module: str, qualname: str, source_file: str) -> SpecEntry:
    return SpecEntry(
        kind="test",
        spec_ref=normalize_spec_ref(f"{module}:{qualname}"),
        module=module,
        qualname=qualname,
        source_file=source_file,
        obj=object(),
        decorator_kwargs={},
    )


class GoodBackend(GeneratorBackend):
    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> tuple[str, None]:
        lines: list[str] = []
        for name in ctx.expected_names:
            lines.append(f"def {name}() -> None:\n    assert True\n")
        return "\n".join(lines).rstrip() + "\n", None


class BadBackend(GeneratorBackend):
    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> tuple[str, None]:
        return "def not_the_expected_test() -> None:\n    assert True\n", None


def _make_cli_test_project(root: Path, *, test_root: str = "tests") -> tuple[Path, str]:
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
                f'test_roots = ["{test_root}"]',
                'generated_dir = "__generated__"',
                "",
                "[test]",
                'pytest_args = ["-q"]',
                "",
            ]
        ),
    )
    (project / "src").mkdir(parents=True, exist_ok=True)
    _write_package_init(project, test_root)
    spec_file = project / test_root / "specs_mod.py"
    _write(
        spec_file,
        "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import jaunt",
                "",
                "@jaunt.test()",
                "def test_generated_smoke() -> None:",
                '    """Generated tests should run."""',
                '    raise AssertionError("spec stub")',
                "",
            ]
        ),
    )
    return project, ".".join(Path(test_root).parts)


def test_cli_test_json_reports_generation_failures(tmp_path: Path, monkeypatch, capsys) -> None:
    project, prefix = _make_cli_test_project(tmp_path)
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs_mod": sys.modules.get(f"{prefix}.specs_mod"),
    }
    orig_sys_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: BadBackend())

    try:
        rc = jaunt.cli.main(
            [
                "test",
                "--root",
                str(project),
                "--no-build",
                "--no-run",
                "--json",
            ]
        )
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    assert rc == jaunt.cli.EXIT_GENERATION_ERROR
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["exit_code"] == jaunt.cli.EXIT_GENERATION_ERROR
    assert payload["generation_failed"]


def test_cli_test_honors_nondefault_test_root(tmp_path: Path, monkeypatch) -> None:
    project, prefix = _make_cli_test_project(tmp_path, test_root="t")
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs_mod": sys.modules.get(f"{prefix}.specs_mod"),
    }
    orig_sys_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: GoodBackend())

    try:
        rc = jaunt.cli.main(
            [
                "test",
                "--root",
                str(project),
                "--no-build",
                "--no-run",
            ]
        )
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    assert rc == jaunt.cli.EXIT_OK
    assert (project / "t" / "__generated__" / "specs_mod.py").exists()


def test_run_tests_no_generate_uses_existing_generated_files(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "tests").mkdir(parents=True, exist_ok=True)

    spec_path = project / "tests" / "specs_mod.py"
    _write(
        spec_path,
        """
def test_generated() -> None:
    raise AssertionError("spec stub")
""".lstrip(),
    )
    generated_path = project / "tests" / "__generated__" / "specs_mod.py"
    _write(
        generated_path,
        """
def test_generated() -> None:
    assert False
""".lstrip(),
    )

    entry = _entry(module="tests.specs_mod", qualname="test_generated", source_file=str(spec_path))
    result = asyncio.run(
        run_tests(
            project_dir=project,
            tests_package="tests",
            generated_dir="__generated__",
            test_roots=[project / "tests"],
            module_specs={"tests.specs_mod": [entry]},
            no_generate=True,
            pythonpath=[project],
            cwd=project,
        )
    )

    assert result.exit_code != 0
    assert result.failed is True


def test_run_test_generation_raises_on_cycle(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "tests").mkdir(parents=True, exist_ok=True)

    a_path = project / "tests" / "a.py"
    b_path = project / "tests" / "b.py"
    _write(a_path, "def test_a() -> None:\n    raise AssertionError\n")
    _write(b_path, "def test_b() -> None:\n    raise AssertionError\n")

    a_entry = _entry(module="tests.a", qualname="test_a", source_file=str(a_path))
    b_entry = _entry(module="tests.b", qualname="test_b", source_file=str(b_path))
    specs = {a_entry.spec_ref: a_entry, b_entry.spec_ref: b_entry}
    spec_graph = build_spec_graph(specs, infer_default=False)

    with pytest.raises(JauntDependencyCycleError):
        asyncio.run(
            run_test_generation(
                project_dir=project,
                tests_package="tests",
                generated_dir="__generated__",
                test_roots=[project / "tests"],
                module_specs={"tests.a": [a_entry], "tests.b": [b_entry]},
                specs=specs,
                spec_graph=spec_graph,
                module_dag={"tests.a": {"tests.b"}, "tests.b": {"tests.a"}},
                stale_modules={"tests.a", "tests.b"},
                backend=GoodBackend(),
                jobs=1,
            )
        )


def test_evict_modules_for_import_reloads_same_named_module_from_new_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_root = tmp_path / "first" / "src"
    second_root = tmp_path / "second" / "src"
    _write(first_root / "pkg" / "__init__.py", "")
    _write(first_root / "pkg" / "mod.py", "VALUE = 1\n")
    _write(second_root / "pkg" / "__init__.py", "")
    _write(second_root / "pkg" / "mod.py", "VALUE = 2\n")

    before = {"pkg": sys.modules.get("pkg"), "pkg.mod": sys.modules.get("pkg.mod")}

    monkeypatch.syspath_prepend(str(first_root))
    import pkg.mod as first_mod  # type: ignore[import-not-found]

    assert first_mod.VALUE == 1

    monkeypatch.syspath_prepend(str(second_root))
    evict_modules_for_import(module_names=["pkg", "pkg.mod"], roots=[second_root])
    import pkg.mod as second_mod  # type: ignore[import-not-found]

    assert second_mod.VALUE == 2
    _restore_modules(["pkg"], before=before)


def test_filter_spec_files_honors_custom_generated_dir() -> None:
    changed = frozenset({Path("/project/t/__gen__/specs_mod.py")})
    result = filter_spec_files(
        changed,
        source_roots=[Path("/project/src")],
        test_roots=[Path("/project/t")],
        generated_dir="__gen__",
    )
    assert result == frozenset()


def test_build_cycle_runner_propagates_no_cache(monkeypatch) -> None:
    calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(
        jaunt.cli, "cmd_build",
        lambda args: calls.append(("build", bool(args.no_cache))) or 0,
    )
    monkeypatch.setattr(
        jaunt.cli, "cmd_test",
        lambda args: calls.append(("test", bool(args.no_cache))) or 0,
    )

    args = jaunt.cli.parse_args(["watch", "--test", "--no-cache"])
    runner = build_cycle_runner(args, run_tests=True)
    result = runner(
        WatchEvent(
            changed_paths=frozenset({Path("/project/src/specs.py")}),
            timestamp=0.0,
        )
    )

    assert result.build_exit_code == 0
    assert result.test_exit_code == 0
    assert calls == [("build", True), ("test", True)]


def test_build_spec_graph_infers_relative_from_import(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    _write(pkg / "__init__.py", "")
    helper_path = pkg / "helpers.py"
    user_path = pkg / "user.py"
    _write(
        helper_path,
        """
def foo() -> int:
    return 1
""".lstrip(),
    )
    _write(
        user_path,
        """
from .helpers import foo

def bar() -> int:
    return foo()
""".lstrip(),
    )

    foo_entry = SpecEntry(
        kind="magic",
        spec_ref=normalize_spec_ref("pkg.helpers:foo"),
        module="pkg.helpers",
        qualname="foo",
        source_file=str(helper_path),
        obj=object(),
        decorator_kwargs={},
    )
    bar_entry = SpecEntry(
        kind="magic",
        spec_ref=normalize_spec_ref("pkg.user:bar"),
        module="pkg.user",
        qualname="bar",
        source_file=str(user_path),
        obj=object(),
        decorator_kwargs={},
    )

    graph = build_spec_graph(
        {foo_entry.spec_ref: foo_entry, bar_entry.spec_ref: bar_entry},
        infer_default=True,
        source_roots=[tmp_path],
    )

    assert foo_entry.spec_ref in graph[bar_entry.spec_ref]
