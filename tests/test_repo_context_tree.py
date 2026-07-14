from pathlib import Path

from jaunt.config import load_config
from jaunt.repo_context import api as api_mod
from jaunt.repo_context import tree as tree_mod
from jaunt.repo_context.digests import TreeCache


def _project(tmp_path: Path) -> Path:
    src = tmp_path / "src" / "pkg"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text('"""Pkg."""\n', encoding="utf-8")
    (src / "a.py").write_text('"""Module A."""\n', encoding="utf-8")
    return tmp_path


def test_sync_adds_entries_and_signature_stable(tmp_path: Path) -> None:
    root = _project(tmp_path)
    cache = TreeCache(root / ".jaunt" / "tree-cache.json")
    doc, result = tree_mod.sync(
        repo_root=root,
        source_roots=[root / "src"],
        generated_dir="__generated__",
        cache=cache,
        project_name="pkg",
        project_version="0.0.0",
        today="2026-06-29",
    )
    assert "src/pkg/a.py" in result.added
    sig1 = doc.signature()
    doc2, _ = tree_mod.sync(
        repo_root=root,
        source_roots=[root / "src"],
        generated_dir="__generated__",
        cache=cache,
        project_name="pkg",
        project_version="0.0.0",
        today="2026-06-30",
    )
    assert doc2.signature() == sig1  # description content unchanged -> stable


def test_sync_drops_ghosts_and_write_skips_when_unchanged(tmp_path: Path) -> None:
    root = _project(tmp_path)
    cache = TreeCache(root / ".jaunt" / "tree-cache.json")
    doc, _ = tree_mod.sync(
        repo_root=root,
        source_roots=[root / "src"],
        generated_dir="__generated__",
        cache=cache,
        project_name="pkg",
        project_version="0.0.0",
        today="2026-06-29",
    )
    out = root / "treedocs.yaml"
    assert doc.write(out) is True
    # Second identical write is a no-op (no churn).
    doc2 = tree_mod.TreeDoc.load(out)
    assert doc2.write(out) is False

    (root / "src" / "pkg" / "a.py").unlink()
    cache2 = TreeCache(root / ".jaunt" / "tree-cache.json")
    doc3, result = tree_mod.sync(
        repo_root=root,
        source_roots=[root / "src"],
        generated_dir="__generated__",
        cache=cache2,
        project_name="pkg",
        project_version="0.0.0",
        today="2026-06-29",
    )
    assert "src/pkg/a.py" in result.removed
    assert "src/pkg/a.py" not in doc3.paths()


def test_build_map_is_read_only_and_explicit_sync_preserves_committed_name(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tool-created-worktree-name"
    _project(root)
    (root / "jaunt.toml").write_text(
        'version = 1\n[paths]\nsource_roots = ["src"]\n[context]\nrepo_map = true\n',
        encoding="utf-8",
    )
    committed = tree_mod.TreeDoc(
        project_name="canonical-project",
        project_version="1",
        last_updated="2026-07-01",
        tree={"src": {"_doc": "source", "pkg": {"_doc": "package"}}},
    )
    path = root / "treedocs.yaml"
    committed.write(path)
    before = path.read_bytes()
    (root / "src" / "pkg" / "new.py").write_text('"""New."""\n', encoding="utf-8")
    cfg = load_config(root=root)

    block = api_mod.repo_map_block_for_build(root=root, cfg=cfg, today="2026-07-13")

    assert "new.py" not in block
    assert path.read_bytes() == before

    api_mod.sync_tree(root=root, cfg=cfg, today="2026-07-13")
    refreshed = tree_mod.TreeDoc.load(path)
    assert refreshed.project_name == "canonical-project"
    assert "src/pkg/new.py" in refreshed.paths()
