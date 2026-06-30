from pathlib import Path

from jaunt.repo_context.digests import TreeCache, source_digest


def test_source_digest_changes_with_content(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    d1 = source_digest(f)
    f.write_text("x = 2\n", encoding="utf-8")
    d2 = source_digest(f)
    assert d1 != d2
    assert len(d1) == 64


def test_tree_cache_roundtrip_and_prune(tmp_path: Path) -> None:
    cache = TreeCache(tmp_path / ".jaunt" / "tree-cache.json")
    cache.set("src/a.py", source_digest="aa", description="does a", enriched=False)
    cache.set("src/b.py", source_digest="bb", description="does b", enriched=True)
    cache.save()

    reloaded = TreeCache(tmp_path / ".jaunt" / "tree-cache.json")
    rec = reloaded.get("src/a.py")
    assert rec is not None and rec.description == "does a" and rec.enriched is False
    reloaded.prune(keep={"src/a.py"})
    assert reloaded.get("src/b.py") is None
