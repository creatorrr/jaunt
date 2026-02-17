from __future__ import annotations

from pathlib import Path

from jaunt.agent_docs import ensure_agent_docs


def test_ensure_agent_docs_creates_agents_md_and_claude_md_symlink(tmp_path: Path) -> None:
    gen = tmp_path / "__generated__"
    gen.mkdir()

    ensure_agent_docs(gen)

    agents_md = gen / "AGENTS.md"
    claude_md = gen / "CLAUDE.md"

    assert agents_md.exists()
    assert claude_md.is_symlink()
    assert claude_md.resolve() == agents_md.resolve()

    content = agents_md.read_text(encoding="utf-8")
    assert "Do Not Edit" in content
    assert "jaunt build" in content


def test_ensure_agent_docs_is_idempotent(tmp_path: Path) -> None:
    gen = tmp_path / "__generated__"
    gen.mkdir()

    ensure_agent_docs(gen)
    ensure_agent_docs(gen)  # second call should not raise

    assert (gen / "AGENTS.md").exists()
    assert (gen / "CLAUDE.md").is_symlink()


def test_ensure_agent_docs_skips_nonexistent_dir(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    # Should not raise even when the directory doesn't exist.
    ensure_agent_docs(missing)
    assert not missing.exists()


def test_write_generated_module_creates_agent_docs(tmp_path: Path) -> None:
    """write_generated_module should place AGENTS.md inside __generated__/."""
    from jaunt.builder import write_generated_module

    pkg = tmp_path / "pkg"
    pkg.mkdir()

    write_generated_module(
        package_dir=tmp_path,
        generated_dir="__generated__",
        module_name="pkg.foo",
        source="x = 1\n",
        header_fields={
            "tool_version": "0",
            "kind": "build",
            "source_module": "pkg.foo",
            "module_digest": "sha256:abc",
            "spec_refs": ["pkg.foo:x"],
        },
    )

    gen_dir = tmp_path / "pkg" / "__generated__"
    assert (gen_dir / "AGENTS.md").exists()
    assert (gen_dir / "CLAUDE.md").is_symlink()


def test_write_generated_test_module_creates_agent_docs(tmp_path: Path) -> None:
    """_write_generated_test_module should place AGENTS.md inside __generated__/."""
    from jaunt.tester import _write_generated_test_module

    project = tmp_path / "proj"
    (project / "tests").mkdir(parents=True)

    _write_generated_test_module(
        project_dir=project,
        tests_package="tests",
        generated_dir="__generated__",
        module_name="tests.test_stuff",
        source="def test_stuff():\n    assert True\n",
        header_fields={
            "tool_version": "0",
            "kind": "test",
            "source_module": "tests.test_stuff",
            "module_digest": "sha256:abc",
            "spec_refs": ["tests.test_stuff:test_stuff"],
        },
    )

    gen_dir = project / "tests" / "__generated__"
    assert (gen_dir / "AGENTS.md").exists()
    assert (gen_dir / "CLAUDE.md").is_symlink()
