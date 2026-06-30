"""High-level repo-context entry points used by the CLI and build path."""

from __future__ import annotations

from pathlib import Path

from jaunt.repo_context import block as block_mod
from jaunt.repo_context import tree as tree_mod
from jaunt.repo_context.digests import TreeCache


def _source_roots(root: Path, cfg) -> list[Path]:
    return [root / sr for sr in cfg.paths.source_roots]


def sync_tree(*, root: Path, cfg, today: str, enrich: bool | None = None):
    cache = TreeCache(root / ".jaunt" / "tree-cache.json")
    doc, result = tree_mod.sync(
        repo_root=root,
        source_roots=_source_roots(root, cfg),
        generated_dir=cfg.paths.generated_dir,
        cache=cache,
        project_name=root.name,
        project_version=str(cfg.version),
        today=today,
    )
    cache.save()
    doc.write(root / cfg.context.repo_map_file)
    return doc, result


def repo_map_block_for_build(*, root: Path, cfg, today: str) -> str:
    if not cfg.context.repo_map:
        return ""
    try:
        doc, _ = sync_tree(root=root, cfg=cfg, today=today)
        return block_mod.render_repo_map(doc, max_chars=cfg.context.max_chars)
    except Exception:  # noqa: BLE001 - never block the build on map maintenance
        return ""


def check_drift(*, root: Path, cfg):
    import shutil

    path = root / cfg.context.repo_map_file
    if not path.exists():
        return tree_mod.SyncResult(added=["<treedocs.yaml missing>"])
    doc = tree_mod.TreeDoc.load(path)

    # Probe a throwaway copy of the real cache so added/removed/restaled counts
    # are accurate without mutating the canonical sidecar (--check is read-only).
    real_cache = root / ".jaunt" / "tree-cache.json"
    probe_path = root / ".jaunt" / "_drift_probe.json"
    probe_path.parent.mkdir(parents=True, exist_ok=True)
    if real_cache.exists():
        shutil.copyfile(real_cache, probe_path)
    elif probe_path.exists():
        probe_path.unlink()

    fresh, result = tree_mod.sync(
        repo_root=root,
        source_roots=_source_roots(root, cfg),
        generated_dir=cfg.paths.generated_dir,
        cache=TreeCache(probe_path),
        project_name=root.name,
        project_version=str(cfg.version),
        today=doc.last_updated,
    )
    if result.added or result.removed or result.restaled or fresh.signature() != doc.signature():
        return (
            result
            if (result.added or result.removed or result.restaled)
            else tree_mod.SyncResult(restaled=["<signature mismatch>"])
        )
    return None
