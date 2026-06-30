from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import jaunt.cli
import jaunt.generate.codex_backend as codex_backend
from test_regressions_review_fixes import (
    GoodBackend,
    _make_cli_test_project,
    _restore_modules,
    _write,
    _write_package_init,
)


def _spec_source(docstring: str, *, extra_blank_after_decorator: bool = False) -> str:
    blank_after_decorator = [""] if extra_blank_after_decorator else []
    return "\n".join(
        [
            "from __future__ import annotations",
            "",
            "import jaunt",
            "",
            "@jaunt.magic()",
            *blank_after_decorator,
            "def generated_smoke() -> None:",
            f'    """{docstring}"""',
            '    raise RuntimeError("spec stub")',
            "",
        ]
    )


def _make_build_project(
    root: Path,
    *,
    docstring: str = "Generate a no-op smoke function.",
    extra_toml: str = "",
) -> tuple[Path, str]:
    project = root / "proj"
    project.mkdir(parents=True, exist_ok=True)
    toml = "\n".join(
        [
            "version = 1",
            "",
            "[paths]",
            'source_roots = ["src"]',
            'test_roots = ["tests"]',
            'generated_dir = "__generated__"',
            "",
        ]
    )
    if extra_toml:
        toml += extra_toml
    _write(project / "jaunt.toml", toml)
    _write_package_init(project, "src/app")
    _write(project / "src" / "app" / "specs.py", _spec_source(docstring))
    return project, "app"


def _write_spec(
    project: Path,
    docstring: str,
    *,
    extra_blank_after_decorator: bool = False,
) -> None:
    _write(
        project / "src" / "app" / "specs.py",
        _spec_source(docstring, extra_blank_after_decorator=extra_blank_after_decorator),
    )


class _GateSpy:
    def __init__(self, verdict: str = "EQUIVALENT") -> None:
        self.verdict = verdict
        self.calls: list[dict[str, object]] = []

    async def __call__(
        self,
        *,
        prompt: str,
        cwd: str,
        sandbox: str,
        model: str,
        reasoning_effort: str,
        extra_config: dict[str, object] | None = None,
    ) -> SimpleNamespace:
        self.calls.append(
            {
                "prompt": prompt,
                "cwd": cwd,
                "sandbox": sandbox,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "extra_config": extra_config,
            }
        )
        return SimpleNamespace(final_message=self.verdict)


def _install_gate_spy(monkeypatch, *, verdict: str = "EQUIVALENT") -> _GateSpy:
    spy = _GateSpy(verdict)
    monkeypatch.setattr(codex_backend, "run_codex_exec", spy)
    return spy


def test_build_json_has_refrozen_field(tmp_path: Path, monkeypatch, capsys) -> None:
    project, prefix = _make_build_project(tmp_path)
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs": sys.modules.get(f"{prefix}.specs"),
    }
    orig_sys_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: GoodBackend())

    try:
        rc = jaunt.cli.main(["build", "--root", str(project), "--json"])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    payload = json.loads(capsys.readouterr().out)
    assert rc == jaunt.cli.EXIT_OK
    assert isinstance(payload["refrozen"], list)
    assert payload["generated"] == ["app.specs"]
    assert "generated" in payload
    assert "skipped" in payload
    assert "failed" in payload


def test_force_rebuilds_all_and_skips_gate(tmp_path: Path, monkeypatch, capsys) -> None:
    project, prefix = _make_build_project(tmp_path)
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs": sys.modules.get(f"{prefix}.specs"),
    }
    orig_sys_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: GoodBackend())

    try:
        rc = jaunt.cli.main(["build", "--root", str(project), "--json"])
        assert rc == jaunt.cli.EXIT_OK
        capsys.readouterr()

        spy = _install_gate_spy(monkeypatch)
        _write_spec(project, "Generate a smoke function with clearly different prose.")
        rc = jaunt.cli.main(["build", "--root", str(project), "--force", "--json"])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    payload = json.loads(capsys.readouterr().out)
    assert rc == jaunt.cli.EXIT_OK
    assert len(spy.calls) == 0
    assert "app.specs" in payload["generated"]
    assert "app.specs" not in payload["refrozen"]


def test_no_semantic_gate_keeps_layer_a_for_cosmetic_edit(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    docstring = "Generate a no-op smoke function.\n\n    It should remain trivial."
    project, prefix = _make_build_project(tmp_path, docstring=docstring)
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs": sys.modules.get(f"{prefix}.specs"),
    }
    orig_sys_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: GoodBackend())

    try:
        rc = jaunt.cli.main(["build", "--root", str(project), "--json"])
        assert rc == jaunt.cli.EXIT_OK
        capsys.readouterr()

        spy = _install_gate_spy(monkeypatch)
        _write_spec(project, docstring, extra_blank_after_decorator=True)
        rc = jaunt.cli.main(["build", "--root", str(project), "--no-semantic-gate", "--json"])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    payload = json.loads(capsys.readouterr().out)
    assert rc == jaunt.cli.EXIT_OK
    assert len(spy.calls) == 0
    assert "app.specs" not in payload["generated"]
    assert "app.specs" not in payload["refrozen"]


def test_no_semantic_gate_rebuilds_prose_only_change_without_gate(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project, prefix = _make_build_project(tmp_path)
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs": sys.modules.get(f"{prefix}.specs"),
    }
    orig_sys_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: GoodBackend())

    try:
        rc = jaunt.cli.main(["build", "--root", str(project), "--json"])
        assert rc == jaunt.cli.EXIT_OK
        capsys.readouterr()

        spy = _install_gate_spy(monkeypatch)
        _write_spec(project, "Generate a no-op smoke function with updated prose.")
        rc = jaunt.cli.main(["build", "--root", str(project), "--no-semantic-gate", "--json"])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    payload = json.loads(capsys.readouterr().out)
    assert rc == jaunt.cli.EXIT_OK
    assert len(spy.calls) == 0
    assert "app.specs" in payload["generated"]
    assert "app.specs" not in payload["refrozen"]


def test_semantic_gate_enabled_false_disables_gate(tmp_path: Path, monkeypatch, capsys) -> None:
    project, prefix = _make_build_project(
        tmp_path,
        extra_toml="\n[semantic_gate]\nenabled = false\n",
    )
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs": sys.modules.get(f"{prefix}.specs"),
    }
    orig_sys_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: GoodBackend())

    try:
        rc = jaunt.cli.main(["build", "--root", str(project), "--json"])
        assert rc == jaunt.cli.EXIT_OK
        capsys.readouterr()

        spy = _install_gate_spy(monkeypatch)
        _write_spec(project, "Generate a no-op smoke function with disabled gate prose.")
        rc = jaunt.cli.main(["build", "--root", str(project), "--json"])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    payload = json.loads(capsys.readouterr().out)
    assert rc == jaunt.cli.EXIT_OK
    assert len(spy.calls) == 0
    assert "app.specs" in payload["generated"]
    assert "app.specs" not in payload["refrozen"]


def test_gate_enabled_equivalent_refreezes_prose_change(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project, prefix = _make_build_project(tmp_path)
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs": sys.modules.get(f"{prefix}.specs"),
    }
    orig_sys_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: GoodBackend())

    try:
        rc = jaunt.cli.main(["build", "--root", str(project), "--json"])
        assert rc == jaunt.cli.EXIT_OK
        capsys.readouterr()

        spy = _install_gate_spy(monkeypatch, verdict="EQUIVALENT")
        _write_spec(project, "Generate a no-op smoke function with equivalent prose.")
        rc = jaunt.cli.main(["build", "--root", str(project), "--json"])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    payload = json.loads(capsys.readouterr().out)
    assert rc == jaunt.cli.EXIT_OK
    assert len(spy.calls) == 1
    assert "app.specs" in payload["refrozen"]
    assert "app.specs" not in payload["generated"]


def test_test_json_has_refrozen_field(tmp_path: Path, monkeypatch, capsys) -> None:
    project, prefix = _make_cli_test_project(tmp_path)
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs_mod": sys.modules.get(f"{prefix}.specs_mod"),
    }
    orig_sys_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: GoodBackend())

    try:
        rc = jaunt.cli.main(["test", "--root", str(project), "--no-build", "--no-run", "--json"])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    payload = json.loads(capsys.readouterr().out)
    assert rc == jaunt.cli.EXIT_OK
    assert isinstance(payload["refrozen"], list)
