from pathlib import Path

from jaunt.repo_context.describe import ast_describe, describe_dir


def test_describe_uses_module_docstring(tmp_path: Path) -> None:
    f = tmp_path / "m.py"
    f.write_text('"""First line of doc.\n\nMore."""\n\ndef foo():\n    pass\n', encoding="utf-8")
    assert ast_describe(f) == "First line of doc."


def test_describe_synthesizes_from_public_names(tmp_path: Path) -> None:
    f = tmp_path / "m.py"
    f.write_text("def foo():\n    pass\n\nclass Bar:\n    pass\n", encoding="utf-8")
    desc = ast_describe(f)
    assert "Bar" in desc and "foo" in desc


def test_describe_caps_length(tmp_path: Path) -> None:
    f = tmp_path / "m.py"
    f.write_text('"""' + "x" * 500 + '"""\n', encoding="utf-8")
    assert len(ast_describe(f, max_len=80)) <= 80


def test_describe_syntax_error_is_safe(tmp_path: Path) -> None:
    f = tmp_path / "m.py"
    f.write_text("def (:\n", encoding="utf-8")
    assert ast_describe(f) == "Python module"


def test_describe_dir_from_init(tmp_path: Path) -> None:
    d = tmp_path / "pkg"
    d.mkdir()
    (d / "__init__.py").write_text('"""The pkg package."""\n', encoding="utf-8")
    assert describe_dir(d) == "The pkg package."
