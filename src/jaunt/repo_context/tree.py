"""treedocs.yaml model + incremental sync (cross-platform, Python-only)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from jaunt.repo_context.describe import ast_describe, describe_dir
from jaunt.repo_context.digests import TreeCache, source_digest

SCHEMA_VERSION = "0.2.0"
_SCHEMA_URL = "https://dandylyons.github.io/treedocs/schemas/0.2.0/treedocs.schema.json"


@dataclass
class SyncResult:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    restaled: list[str] = field(default_factory=list)


@contextlib.contextmanager
def _lock(lock_path: Path, *, timeout: float = 10.0) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fd = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                # Stale lock fallback: proceed without blocking the build forever.
                break
            time.sleep(0.05)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(lock_path)


@dataclass
class TreeDoc:
    project_name: str
    project_version: str
    last_updated: str
    tree: dict  # nested mapping mirroring the filesystem

    @classmethod
    def load(cls, path: Path) -> TreeDoc:
        import yaml  # lazy

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        project = data.get("project", {}) or {}
        return cls(
            project_name=str(project.get("name", "")),
            project_version=str(project.get("version", "")),
            last_updated=str(project.get("last_updated", "")),
            tree=data.get("tree", {}) or {},
        )

    def signature(self) -> str:
        """sha256 over canonical tree descriptions only (manual-edit drift)."""
        canonical = json.dumps(self.tree, sort_keys=True, ensure_ascii=False)
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def paths(self) -> set[str]:
        out: set[str] = set()

        def walk(node: dict, prefix: str) -> None:
            for key, val in node.items():
                if key in ("_doc", "_description", "_references", "_link"):
                    continue
                rel = f"{prefix}/{key}" if prefix else key
                if isinstance(val, dict):
                    out.add(rel)
                    walk(val, rel)
                else:
                    out.add(rel)

        walk(self.tree, "")
        return out

    def write(self, path: Path) -> bool:
        """Atomic write under a lock. Returns False (no write) if unchanged."""
        import yaml  # lazy

        if path.exists():
            with contextlib.suppress(Exception):
                if TreeDoc.load(path).signature() == self.signature():
                    return False
        payload = {
            "schema_version": SCHEMA_VERSION,
            "project": {
                "name": self.project_name,
                "version": self.project_version,
                "last_updated": self.last_updated,
            },
            "signature": self.signature(),
            "tree": self.tree,
        }
        body = f"# yaml-language-server: $schema={_SCHEMA_URL}\n" + yaml.safe_dump(
            payload, sort_keys=True, allow_unicode=True, default_flow_style=False
        )
        with _lock(path.parent / ".jaunt" / "tree.lock"):
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, path)
        return True


def _iter_entries(*, source_roots: list[Path], generated_dir: str) -> list[Path]:
    out: list[Path] = []
    for sr in source_roots:
        if not sr.exists():
            continue
        for p in sr.rglob("*.py"):
            if generated_dir in p.parts or "__pycache__" in p.parts:
                continue
            out.append(p)
    return out


def _insert(tree: dict, parts: list[str], description: str, *, is_dir: bool) -> None:
    node = tree
    for part in parts[:-1]:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):  # a file shadowed a dir name; reset
            node = {}
    leaf = parts[-1]
    if is_dir:
        d = node.setdefault(leaf, {})
        if isinstance(d, dict):
            d["_doc"] = description
    else:
        node[leaf] = description


def sync(
    *,
    repo_root: Path,
    source_roots: list[Path],
    generated_dir: str,
    cache: TreeCache,
    project_name: str,
    project_version: str,
    today: str,
) -> tuple[TreeDoc, SyncResult]:
    result = SyncResult()
    tree: dict = {}
    seen: set[str] = set()
    dirs: set[Path] = set()

    for path in sorted(_iter_entries(source_roots=source_roots, generated_dir=generated_dir)):
        rel = path.resolve().relative_to(repo_root.resolve()).as_posix()
        seen.add(rel)
        digest = source_digest(path)
        rec = cache.get(rel)
        if rec is None:
            result.added.append(rel)
        elif rec.source_digest != digest:
            result.restaled.append(rel)
        description = (
            rec.description
            if rec is not None and rec.source_digest == digest
            else ast_describe(path)
        )
        cache.set(rel, source_digest=digest, description=description, enriched=False)
        _insert(tree, rel.split("/"), description, is_dir=False)
        for parent in path.resolve().parents:
            if parent == repo_root.resolve():
                break
            dirs.add(parent)

    for d in sorted(dirs):
        rel = d.resolve().relative_to(repo_root.resolve()).as_posix()
        if not rel:
            continue
        _insert(tree, rel.split("/"), describe_dir(d), is_dir=True)

    for rel in list(cache._records):  # noqa: SLF001 - prune ghosts
        if rel not in seen:
            result.removed.append(rel)
    cache.prune(keep=seen)
    cache.save()

    return (
        TreeDoc(
            project_name=project_name,
            project_version=project_version,
            last_updated=today,
            tree=tree,
        ),
        result,
    )


def is_drifted(
    treedoc: TreeDoc,
    *,
    repo_root: Path,
    source_roots: list[Path],
    generated_dir: str,
    cache: TreeCache,
) -> bool:
    fresh, result = sync(
        repo_root=repo_root,
        source_roots=source_roots,
        generated_dir=generated_dir,
        cache=TreeCache(repo_root / ".jaunt" / "_drift_probe.json"),
        project_name=treedoc.project_name,
        project_version=treedoc.project_version,
        today=treedoc.last_updated,
    )
    if result.added or result.removed or result.restaled:
        return True
    return fresh.signature() != treedoc.signature()
