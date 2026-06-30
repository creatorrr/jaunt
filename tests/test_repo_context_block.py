from jaunt.repo_context.block import render_repo_map
from jaunt.repo_context.tree import TreeDoc


def _doc() -> TreeDoc:
    return TreeDoc(
        project_name="pkg",
        project_version="0",
        last_updated="2026-06-29",
        tree={"src": {"_doc": "source", "a.py": "module a", "b.py": "module b"}},
    )


def test_render_repo_map_includes_descriptions_no_volatile() -> None:
    out = render_repo_map(_doc())
    assert out.startswith("## Repository map")
    assert "a.py" in out and "module a" in out
    assert "2026-06-29" not in out and "sha256:" not in out  # no volatile fields


def test_render_repo_map_caps() -> None:
    big = {f"f{i}.py": "x" * 50 for i in range(1000)}
    doc = TreeDoc("p", "0", "2026-06-29", big)
    out = render_repo_map(doc, max_chars=500)
    assert len(out) <= 500 + 64  # header + truncation marker slack
    assert "truncated" in out.lower()


def test_render_repo_map_empty_is_empty() -> None:
    assert render_repo_map(TreeDoc("p", "0", "2026-06-29", {})) == ""
