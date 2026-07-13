"""Validated, recoverable write plans for TypeScript artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from jaunt.typescript.protocol import OverlayArtifact, ProtocolValidationError
from jaunt.typescript.workspace import root_relative_path


@dataclass(frozen=True, slots=True)
class ArtifactWrite:
    path: Path
    content: str
    sha256: str
    kind: str
    module_id: str


@dataclass(frozen=True, slots=True)
class ArtifactPlan:
    id: str
    root: Path
    writes: tuple[ArtifactWrite, ...]
    manifest_path: Path


def artifact_plan(root: Path, artifacts: tuple[OverlayArtifact, ...]) -> ArtifactPlan:
    """Validate worker-returned bytes and create a deterministic write set."""

    root = root.resolve()
    writes: list[ArtifactWrite] = []
    seen: set[Path] = set()
    for artifact in sorted(artifacts, key=lambda item: item.path):
        path = root_relative_path(root, artifact.path, label="artifact.path")
        if path in seen:
            raise ProtocolValidationError(f"duplicate artifact path: {artifact.path}")
        seen.add(path)
        digest = hashlib.sha256(artifact.content.encode("utf-8")).hexdigest()
        expected = artifact.sha256.removeprefix("sha256:")
        if digest != expected:
            raise ProtocolValidationError(
                f"artifact hash mismatch for {artifact.path}: expected {expected}, got {digest}"
            )
        writes.append(
            ArtifactWrite(
                path=path,
                content=artifact.content,
                sha256=digest,
                kind=artifact.kind,
                module_id=artifact.module_id,
            )
        )
    identity_payload = "\n".join(
        f"{write.path.relative_to(root).as_posix()}={write.sha256}" for write in writes
    )
    identity = hashlib.sha256(identity_payload.encode()).hexdigest()[:16]
    transaction_id = f"{identity}-{uuid.uuid4().hex[:8]}"
    manifest = root / ".jaunt" / "transactions" / f"{transaction_id}.json"
    return ArtifactPlan(id=transaction_id, root=root, writes=tuple(writes), manifest_path=manifest)


def commit_artifact_plan(plan: ArtifactPlan) -> None:
    """Commit validated writes with a durable recovery manifest.

    Individual replacements are atomic. The manifest intentionally survives a
    partial failure so the next Jaunt command can classify the transaction as
    incomplete instead of treating mixed artifact generations as fresh.
    """

    plan.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_payload = {
        "id": plan.id,
        "state": "prepared",
        "writes": [
            {
                "path": write.path.relative_to(plan.root).as_posix(),
                "sha256": write.sha256,
                "kind": write.kind,
                "moduleId": write.module_id,
            }
            for write in plan.writes
        ],
    }
    _atomic_text(plan.manifest_path, json.dumps(manifest_payload, sort_keys=True, indent=2) + "\n")
    for write in plan.writes:
        write.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_text(write.path, write.content)
    plan.manifest_path.unlink()
    _fsync_directory(plan.manifest_path.parent)


def incomplete_transaction_manifests(root: Path) -> tuple[Path, ...]:
    directory = root.resolve() / ".jaunt" / "transactions"
    if not directory.is_dir():
        return ()
    return tuple(sorted(directory.glob("*.json"), key=lambda path: path.as_posix()))


def _atomic_text(path: Path, content: str) -> None:
    fd, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp", text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _fsync_directory(path: Path) -> None:
    """Persist directory-entry replacements where the host supports it."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Windows and a few network filesystems do not expose directory fsync.
        pass
    finally:
        os.close(descriptor)
