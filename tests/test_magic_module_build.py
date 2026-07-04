"""End-to-end (mocked backend) coverage for jaunt.magic_module builds."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import jaunt.cli
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class _EchoBackend(GeneratorBackend):
    """Emits a real body per expected name so the call result is assertable."""

    @property
    def model_name(self) -> str:
        return "echo"

    @property
    def provider_name(self) -> str:
        return "echo"

    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> tuple[str, None]:
        lines = [
            f"def {name}(*args, **kwargs):\n    return {name!r}\n" for name in ctx.expected_names
        ]
        return "\n".join(lines).rstrip() + "\n", None


def _purge(names: list[str]) -> None:
    from jaunt import registry

    registry.clear_registries()
    for key in list(sys.modules):
        if key == "__generated__" or key.startswith("__generated__."):
            del sys.modules[key]
    for name in names:
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key, None)


def _governed_project(root: Path) -> Path:
    project = root / "proj"
    _write(
        project / "jaunt.toml",
        'version = 1\n\n[paths]\nsource_roots = ["src"]\n\n[build]\nemit_stubs = true\n',
    )
    _write(
        project / "src" / "gm_mod.py",
        "import jaunt\n\n"
        'jaunt.magic_module(__name__, prompt="be terse")\n\n\n'
        "def parse_email(raw: str) -> str:\n"
        '    """Parse an email into its local part."""\n'
        "    ...\n\n\n"
        "def normalize(raw: str) -> str:\n"
        '    """Normalize an email to lowercase."""\n'
        "    ...\n",
    )
    return project


def test_governed_module_builds_validates_emits_pyi_and_check_green(tmp_path, monkeypatch):
    project = _governed_project(tmp_path)
    orig_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: _EchoBackend())
    try:
        rc = jaunt.cli.main(["build", "--root", str(project)])
        assert rc == jaunt.cli.EXIT_OK
        gen = project / "src" / "__generated__" / "gm_mod.py"
        assert gen.exists()
        assert "def parse_email" in gen.read_text()
        assert (project / "src" / "gm_mod.pyi").exists()
        # jaunt check (no model) is green right after a build.
        assert jaunt.cli.main(["check", "--root", str(project)]) == jaunt.cli.EXIT_OK
    finally:
        sys.path[:] = orig_path
        _purge(["gm_mod"])


def test_governed_module_runtime_returns_built_result(tmp_path, monkeypatch):
    project = _governed_project(tmp_path)
    orig_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: _EchoBackend())
    try:
        assert jaunt.cli.main(["build", "--root", str(project)]) == jaunt.cli.EXIT_OK
        # Fresh import of the governed module resolves attribute access to the built code.
        _purge(["gm_mod"])
        sys.path.insert(0, str(project / "src"))
        importlib.invalidate_caches()
        mod = importlib.import_module("gm_mod")
        assert mod.parse_email("a@b.com") == "parse_email"
        assert mod.normalize("A@B") == "normalize"
    finally:
        sys.path[:] = orig_path
        _purge(["gm_mod"])


def test_mixed_module_builds(tmp_path, monkeypatch):
    project = tmp_path / "mix"
    _write(project / "jaunt.toml", 'version = 1\n\n[paths]\nsource_roots = ["src"]\n')
    _write(
        project / "src" / "mixed_mod.py",
        "import jaunt\n\n"
        'jaunt.magic_module(__name__, prompt="be terse")\n\n\n'
        "def module_spec(x: int) -> int:\n"
        '    """Module-style spec."""\n'
        "    ...\n\n\n"
        '@jaunt.magic(prompt="mine")\n'
        "def decorated_spec(x: int) -> int:\n"
        '    """Decorated spec."""\n'
        "    ...\n\n\n"
        "def helper(x: int) -> int:\n"
        "    return module_spec(x) + 1\n",
    )
    orig_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: _EchoBackend())
    try:
        rc = jaunt.cli.main(["build", "--root", str(project), "--json"])
    finally:
        sys.path[:] = orig_path
        _purge(["mixed_mod"])
    assert rc == jaunt.cli.EXIT_OK
    gen = project / "src" / "__generated__" / "mixed_mod.py"
    assert gen.exists()
    body = gen.read_text()
    assert "def module_spec" in body and "def decorated_spec" in body
    # helper is handwritten (real body) and must not be generated.
    assert "def helper" not in body


def test_fresh_scaffold_builds_with_mocked_backend(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    jaunt.cli.cmd_init(jaunt.cli.parse_args(["init"]))
    orig_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: _EchoBackend())
    try:
        rc = jaunt.cli.main(["build", "--root", str(tmp_path), "--json"])
        out = capsys.readouterr().out
    finally:
        sys.path[:] = orig_path
        _purge(["specs"])
    data = json.loads(out)
    assert rc == jaunt.cli.EXIT_OK
    assert data["ok"] is True
    assert data["generated"] == ["specs"]


def test_fresh_stub_bytes_never_rewritten_across_environments(tmp_path, monkeypatch):
    """A digest-fresh stub is left byte-for-byte alone on later builds, even when
    this environment would render it differently (ruff present vs absent) —
    committed stubs must not churn across machines. (1.4.2 codex review.)"""
    project = _governed_project(tmp_path)
    orig_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: _EchoBackend())
    try:
        assert jaunt.cli.main(["build", "--root", str(project)]) == jaunt.cli.EXIT_OK
        stub = project / "src" / "gm_mod.pyi"
        assert stub.exists()

        # Simulate a different environment's rendering: cosmetic byte change,
        # header (and therefore inputs digest) untouched.
        mangled = stub.read_text(encoding="utf-8") + "\n\n"
        stub.write_text(mangled, encoding="utf-8")

        _purge(["gm_mod"])
        assert jaunt.cli.main(["build", "--root", str(project)]) == jaunt.cli.EXIT_OK
        assert stub.read_text(encoding="utf-8") == mangled  # untouched
    finally:
        sys.path[:] = orig_path
        _purge(["gm_mod"])
