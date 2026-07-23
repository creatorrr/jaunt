"""Focused durability tests for TypeScript artifact transactions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import stat
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from jaunt.errors import JauntGenerationError
from jaunt.typescript import builder as ts_builder
from jaunt.typescript import tester as ts_tester
from jaunt.typescript.builder import (
    MISSING_INPUT,
    _recover_atomic_write_manifests,
    _TRANSACTION_SCHEME,
    _Write,
    atomic_write_manifest,
)
from jaunt.typescript.status import classify_modules


def _digest(content: str) -> str:
    return f"sha256:{hashlib.sha256(content.encode()).hexdigest()}"


def _transaction_manifest(
    root: Path,
    *,
    name: str = "ts-crash.json",
    state: str,
    writes: list[dict[str, Any]],
    legacy: bool = False,
) -> Path:
    manifest = root / ".jaunt" / "transactions" / name
    manifest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        **({} if legacy else {"scheme": _TRANSACTION_SCHEME}),
        "state": state,
        "writes": writes,
    }
    manifest.write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def _unresolved_transaction_marker(root: Path, name: str) -> Path:
    manifest = root / ".jaunt" / "transactions" / name
    manifest.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any]
    if name.startswith("test-repair-"):
        payload = {
            "scheme": "jaunt-ts-test-repair/2",
            "ownerPid": os.getpid(),
            "snapshots": [],
        }
    elif name.startswith("ts-"):
        payload = {
            "scheme": _TRANSACTION_SCHEME,
            "state": "prepared",
            "writes": [],
        }
    else:
        payload = {"state": "prepared", "writes": []}
    manifest.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def test_required_directory_sync_uses_windows_native_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []

    def reject_posix_open(*_args, **_kwargs):
        raise AssertionError("POSIX directory open must not run on Windows")

    monkeypatch.setattr(ts_builder.os, "name", "nt")
    monkeypatch.setattr(ts_builder.os, "open", reject_posix_open)
    monkeypatch.setattr(ts_builder, "_fsync_directory_windows", calls.append)

    ts_builder._fsync_directory_required(tmp_path)

    assert calls == [tmp_path]


def test_required_directory_sync_keeps_posix_open_and_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[tuple[Path, int]] = []
    synced: list[int] = []
    closed: list[int] = []

    monkeypatch.setattr(ts_builder.os, "name", "posix")
    monkeypatch.setattr(
        ts_builder.os,
        "open",
        lambda path, flags: opened.append((Path(path), flags)) or 47,
    )
    monkeypatch.setattr(ts_builder.os, "fsync", synced.append)
    monkeypatch.setattr(ts_builder.os, "close", closed.append)

    ts_builder._fsync_directory_required(tmp_path)

    assert opened == [(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))]
    assert synced == [47]
    assert closed == [47]


def test_durable_directory_creation_syncs_new_ancestors_in_syscall_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    target = existing / "generated" / "nested"
    events: list[tuple[str, str | Path]] = []
    original_mkdir = os.mkdir
    original_sync = ts_builder._PinnedDirectory.fsync_required

    def recording_mkdir(path, *args, **kwargs) -> None:
        events.append(("mkdir", str(path)))
        original_mkdir(path, *args, **kwargs)

    def recording_sync(directory: ts_builder._PinnedDirectory) -> None:
        events.append(("sync", directory.path))
        original_sync(directory)

    monkeypatch.setattr(ts_builder.os, "mkdir", recording_mkdir)
    monkeypatch.setattr(ts_builder._PinnedDirectory, "fsync_required", recording_sync)

    created = ts_builder._ensure_durable_directory(tmp_path, target)

    generated = existing / "generated"
    assert created == (generated, target)
    assert events == [
        ("mkdir", "existing"),
        ("sync", tmp_path),
        ("mkdir", "generated"),
        ("sync", existing),
        ("mkdir", "nested"),
        ("sync", generated),
    ]


def test_durable_directory_creation_stops_before_a_deeper_mkdir_when_sync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "one" / "two"

    def fail_required_sync(directory: ts_builder._PinnedDirectory) -> None:
        raise OSError(f"could not sync {directory.path}")

    monkeypatch.setattr(ts_builder._PinnedDirectory, "fsync_required", fail_required_sync)

    with pytest.raises(OSError, match="could not sync"):
        ts_builder._ensure_durable_directory(tmp_path, target)

    assert (tmp_path / "one").is_dir()
    assert not target.exists()


def test_pinned_workspace_deduplicates_confirmed_existing_ancestors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    synced_parents: list[Path] = []

    def recording_sync(directory: ts_builder._PinnedDirectory) -> None:
        synced_parents.append(directory.path)
        assert directory.descriptor is not None
        os.fsync(directory.descriptor)

    monkeypatch.setattr(ts_builder._PinnedDirectory, "fsync_required", recording_sync)
    with ts_builder._PinnedWorkspace(tmp_path) as workspace:
        workspace.directory(existing / "one")
        workspace.directory(existing / "two")

    assert synced_parents == [tmp_path, existing, existing]


def test_durable_directory_creation_rejects_symlink_inserted_during_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    redirect = tmp_path / "redirect"
    original_mkdir = os.mkdir
    inserted = False

    def insert_redirect(path, *args, **kwargs) -> None:
        nonlocal inserted
        if path == "redirect" and not inserted:
            os.symlink(
                outside,
                "redirect",
                target_is_directory=True,
                dir_fd=kwargs["dir_fd"],
            )
            inserted = True
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(ts_builder.os, "mkdir", insert_redirect)

    with pytest.raises(JauntGenerationError, match="redirecting workspace directory"):
        ts_builder._ensure_durable_directory(tmp_path, redirect / "nested")

    assert inserted is True
    assert redirect.is_symlink()
    assert not (outside / "nested").exists()


def test_durable_directory_creation_rejects_windows_reparse_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(JauntGenerationError, match="redirecting workspace entry"):
        ts_builder._validate_windows_pinned_attributes(
            tmp_path / "junction",
            stat.FILE_ATTRIBUTE_REPARSE_POINT | 0x10,
            directory=True,
        )


def test_atomic_write_syncs_new_directory_entries_before_using_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, Path]] = []
    original_sync = ts_builder._PinnedDirectory.fsync_required
    original_create_temp = ts_builder._PinnedDirectory.create_temp

    def recording_sync(directory: ts_builder._PinnedDirectory) -> None:
        events.append(("sync", directory.path))
        original_sync(directory)

    def recording_create_temp(
        directory: ts_builder._PinnedDirectory, prefix: str, suffix: str = ""
    ):
        events.append(("temp", directory.path))
        return original_create_temp(directory, prefix, suffix)

    monkeypatch.setattr(ts_builder._PinnedDirectory, "fsync_required", recording_sync)
    monkeypatch.setattr(ts_builder._PinnedDirectory, "create_temp", recording_create_temp)

    atomic_write_manifest(
        tmp_path,
        (_Write("generated/nested/value.ts", "export const value = 1;\n", "api", "ts:value"),),
    )

    generated = tmp_path / "generated"
    output_directory = generated / "nested"
    transaction_directory = tmp_path / ".jaunt" / "transactions"
    assert events[:6] == [
        ("sync", tmp_path),
        ("sync", generated),
        ("temp", output_directory),
        ("sync", tmp_path),
        ("sync", tmp_path / ".jaunt"),
        ("temp", transaction_directory),
    ]


@pytest.mark.skipif(os.name == "nt", reason="Windows pinned handles deny the rename itself")
def test_atomic_write_aborts_when_pinned_parent_is_rebound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_directory = tmp_path / "out"
    output_directory.mkdir()
    anchored_directory = tmp_path / "pinned-original"
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    original_create_temp = ts_builder._PinnedDirectory.create_temp
    swapped = False

    def swap_parent_before_staging(
        directory: ts_builder._PinnedDirectory, prefix: str, suffix: str = ""
    ):
        nonlocal swapped
        if directory.path == output_directory and not swapped:
            output_directory.rename(anchored_directory)
            output_directory.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_create_temp(directory, prefix, suffix)

    monkeypatch.setattr(
        ts_builder._PinnedDirectory,
        "create_temp",
        swap_parent_before_staging,
    )
    try:
        with pytest.raises(JauntGenerationError, match="no longer bound"):
            atomic_write_manifest(
                tmp_path,
                (_Write("out/value.ts", "safe\n", "implementation", "ts:value"),),
            )

        assert swapped is True
        assert not (outside / "value.ts").exists()
        assert not (anchored_directory / "value.ts").exists()
    finally:
        outside.rmdir()


def test_atomic_write_cleans_registered_stage_when_file_fsync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_directory = tmp_path / "out"
    output_directory.mkdir()
    output = output_directory / "value.ts"
    output.write_text("old\n", encoding="utf-8")
    original_fsync = os.fsync

    def fail_regular_file_sync(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("simulated staged-file fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(ts_builder.os, "fsync", fail_regular_file_sync)

    with pytest.raises(OSError, match="simulated staged-file fsync failure"):
        atomic_write_manifest(
            tmp_path,
            (_Write("out/value.ts", "new\n", "implementation", "ts:value"),),
        )

    assert output.read_text(encoding="utf-8") == "old\n"
    assert tuple(output_directory.glob(".value.ts.jaunt-*")) == ()


def test_manifest_closes_temp_descriptor_when_fdopen_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction_directory = tmp_path / ".jaunt/transactions"
    captured: list[int] = []
    original_create_temp = ts_builder._PinnedDirectory.create_temp

    def capture_temp(
        directory: ts_builder._PinnedDirectory, prefix: str, suffix: str = ""
    ) -> tuple[int, str]:
        descriptor, name = original_create_temp(directory, prefix, suffix)
        captured.append(descriptor)
        return descriptor, name

    with ts_builder._PinnedWorkspace(tmp_path) as workspace:
        pinned = workspace.directory(transaction_directory)
        monkeypatch.setattr(ts_builder._PinnedDirectory, "create_temp", capture_temp)
        monkeypatch.setattr(
            ts_builder.os,
            "fdopen",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("fdopen failed")),
        )

        with pytest.raises(OSError, match="fdopen failed"):
            ts_builder._write_transaction_manifest(
                transaction_directory / "ts-fdopen.json",
                {"state": "prepared"},
                pinned_directory=pinned,
            )

    assert len(captured) == 1
    with pytest.raises(OSError):
        os.fstat(captured[0])
    assert tuple(transaction_directory.glob("*.tmp")) == ()


def test_atomic_write_closes_stage_descriptor_when_fdopen_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_directory = tmp_path / "out"
    output_directory.mkdir()
    captured: list[int] = []
    original_create_temp = ts_builder._PinnedDirectory.create_temp

    def capture_stage(
        directory: ts_builder._PinnedDirectory, prefix: str, suffix: str = ""
    ) -> tuple[int, str]:
        descriptor, name = original_create_temp(directory, prefix, suffix)
        if directory.path == output_directory:
            captured.append(descriptor)
        return descriptor, name

    monkeypatch.setattr(ts_builder._PinnedDirectory, "create_temp", capture_stage)
    monkeypatch.setattr(
        ts_builder.os,
        "fdopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("fdopen failed")),
    )

    with pytest.raises(OSError, match="fdopen failed"):
        atomic_write_manifest(
            tmp_path,
            (_Write("out/value.ts", "new\n", "implementation", "ts:value"),),
        )

    assert len(captured) == 1
    with pytest.raises(OSError):
        os.fstat(captured[0])
    assert tuple(output_directory.glob(".value.ts.jaunt-*")) == ()


def test_atomic_write_closes_rollback_descriptor_when_fdopen_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_directory = tmp_path / "out"
    output_directory.mkdir()
    first = output_directory / "a.ts"
    second = output_directory / "b.ts"
    first.write_text("old-a\n", encoding="utf-8")
    second.write_text("old-b\n", encoding="utf-8")
    original_fdopen = os.fdopen
    original_create_temp = ts_builder._PinnedDirectory.create_temp
    original_replace = ts_builder._PinnedDirectory.replace
    rollback_descriptor: int | None = None

    def fail_rollback_fdopen(descriptor: int, *args, **kwargs):
        if descriptor == rollback_descriptor:
            raise OSError("rollback fdopen failed")
        return original_fdopen(descriptor, *args, **kwargs)

    def capture_rollback_temp(
        directory: ts_builder._PinnedDirectory, prefix: str, suffix: str = ""
    ) -> tuple[int, str]:
        nonlocal rollback_descriptor
        descriptor, name = original_create_temp(directory, prefix, suffix)
        if ".rollback-" in prefix:
            rollback_descriptor = descriptor
        return descriptor, name

    def fail_second_replace(
        directory: ts_builder._PinnedDirectory, source: str, destination: str
    ) -> None:
        if directory.path == output_directory and destination == second.name:
            raise OSError("simulated second replacement failure")
        original_replace(directory, source, destination)

    monkeypatch.setattr(ts_builder.os, "fdopen", fail_rollback_fdopen)
    monkeypatch.setattr(ts_builder._PinnedDirectory, "create_temp", capture_rollback_temp)
    monkeypatch.setattr(ts_builder._PinnedDirectory, "replace", fail_second_replace)

    with pytest.raises(OSError, match="simulated second replacement failure"):
        atomic_write_manifest(
            tmp_path,
            (
                _Write("out/a.ts", "new-a\n", "implementation", "ts:a"),
                _Write("out/b.ts", "new-b\n", "implementation", "ts:b"),
            ),
        )

    assert rollback_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(rollback_descriptor)
    assert tuple(output_directory.glob(".a.ts.rollback-*")) == ()


def test_atomic_cleanup_attempts_all_temps_and_releases_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_directory = tmp_path / "out"
    output_directory.mkdir()
    original_unlink = ts_builder._PinnedDirectory.unlink
    original_release = ts_builder._TransactionLease.release
    cleanup_attempts: list[str] = []
    release_calls = 0

    def fail_first_stage_cleanup(
        directory: ts_builder._PinnedDirectory,
        name: str,
        *,
        missing_ok: bool = False,
    ) -> bool:
        if ".jaunt-" in name:
            cleanup_attempts.append(name)
            if name.startswith(".a.ts.jaunt-"):
                raise OSError("simulated temp cleanup failure")
        return original_unlink(directory, name, missing_ok=missing_ok)

    def record_release(lease: ts_builder._TransactionLease) -> None:
        nonlocal release_calls
        release_calls += 1
        original_release(lease)

    def fail_guard() -> None:
        raise RuntimeError("primary guard failure")

    monkeypatch.setattr(ts_builder._PinnedDirectory, "unlink", fail_first_stage_cleanup)
    monkeypatch.setattr(ts_builder._TransactionLease, "release", record_release)

    with pytest.raises(RuntimeError, match="primary guard failure") as raised:
        atomic_write_manifest(
            tmp_path,
            (
                _Write("out/a.ts", "new-a\n", "implementation", "ts:a"),
                _Write("out/b.ts", "new-b\n", "implementation", "ts:b"),
            ),
            pre_commit_guard=fail_guard,
        )

    assert any(name.startswith(".a.ts.jaunt-") for name in cleanup_attempts)
    assert any(name.startswith(".b.ts.jaunt-") for name in cleanup_attempts)
    assert release_calls == 1
    assert any(
        "transaction cleanup also failed" in note for note in getattr(raised.value, "__notes__", ())
    )


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO creation is POSIX-only")
def test_pinned_regular_read_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    output_directory = tmp_path / "out"
    output_directory.mkdir()
    fifo = output_directory / "value.ts"
    os.mkfifo(fifo)

    with ts_builder._PinnedWorkspace(tmp_path) as workspace:
        pinned = workspace.directory(output_directory, create=False)
        with pytest.raises(IsADirectoryError):
            pinned.read_bytes(fifo.name)


def test_transaction_primitives_do_not_recreate_an_unsynced_directory(tmp_path: Path) -> None:
    directory = tmp_path / ".jaunt" / "transactions"

    with pytest.raises(FileNotFoundError, match="Transaction directory does not exist"):
        ts_builder._acquire_transaction_lease(directory, blocking=True)
    with pytest.raises(FileNotFoundError, match="Transaction directory does not exist"):
        ts_builder._write_transaction_manifest(directory / "ts-missing.json", {"state": "prepared"})

    assert not (tmp_path / ".jaunt").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX root-inode lease only")
def test_posix_authority_lease_opens_independent_root_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction_directory = tmp_path / ".jaunt/transactions"

    with ts_builder._PinnedWorkspace(tmp_path) as workspace:
        pinned_directory = workspace.directory(transaction_directory)
        root_descriptor = workspace.root_directory.descriptor
        original_open = os.open
        opened: list[tuple[object, int | None]] = []

        def recording_open(path, flags, *args, **kwargs):
            opened.append((path, kwargs.get("dir_fd")))
            return original_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(ts_builder.os, "open", recording_open)
        lease = ts_builder._acquire_transaction_lease(
            transaction_directory,
            blocking=True,
            pinned_directory=pinned_directory,
            authority_directory=workspace.root_directory,
        )
        assert lease is not None
        try:
            assert lease.descriptor != root_descriptor
            assert opened == [(".", root_descriptor)]
        finally:
            lease.release()


def test_pinned_workspace_create_false_never_creates_missing_components(tmp_path: Path) -> None:
    missing = tmp_path / ".jaunt" / "transactions"

    with ts_builder._PinnedWorkspace(tmp_path) as workspace:
        with pytest.raises(FileNotFoundError):
            workspace.directory(missing, create=False)

    assert not (tmp_path / ".jaunt").exists()


@pytest.mark.parametrize("leaf", ("stream:name", "trailing.", "trailing ", "a/b", "a\\b", "a\0b"))
def test_pinned_directory_rejects_unsafe_cross_platform_leaf_names(leaf: str) -> None:
    with pytest.raises(JauntGenerationError, match="Unsafe pinned-directory leaf"):
        ts_builder._PinnedDirectory._leaf(leaf)


@pytest.mark.parametrize(
    "leaf",
    (
        "CON",
        "nul.txt",
        "NUL .txt",
        "CON .log",
        "COM1.log",
        "lpt²",
        "AUX.anything",
        "question?mark",
    ),
)
def test_pinned_directory_rejects_windows_device_and_invalid_names(leaf: str) -> None:
    with pytest.raises(JauntGenerationError, match="Unsafe pinned-directory leaf"):
        ts_builder._PinnedDirectory._leaf(leaf)


def test_pinned_workspace_rejects_reserved_directory_component(tmp_path: Path) -> None:
    with ts_builder._PinnedWorkspace(tmp_path) as workspace:
        with pytest.raises(JauntGenerationError, match="Unsafe pinned-directory leaf"):
            workspace.directory(tmp_path / "NUL .txt" / "nested")


def test_windows_pinned_file_opens_inherit_nonblocking_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []
    handles = iter((71, 72))

    def open_pinned(_path: Path, **kwargs: object) -> int:
        calls.append(kwargs)
        return next(handles)

    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(open_osfhandle=lambda handle, _flags: handle),
    )
    monkeypatch.setattr(ts_builder, "_windows_open_pinned_path", open_pinned)
    pinned = ts_builder._PinnedDirectory(
        path=tmp_path,
        windows_handle=object(),
        blocking=False,
    )

    assert pinned._open_regular_read("marker.json") == 71
    assert (
        pinned.open_lock(
            ".atomic-write.lock",
            os.O_CREAT | os.O_RDWR,
            0o600,
            blocking=False,
        )
        == 72
    )
    assert [call["blocking"] for call in calls] == [False, False]


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor regression")
def test_pinned_directory_close_detaches_descriptor_before_close_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = os.open(tmp_path, os.O_RDONLY)
    pinned = ts_builder._PinnedDirectory(path=tmp_path, descriptor=descriptor)
    original_close = os.close
    closed: list[int] = []

    def close_then_raise(value: int) -> None:
        closed.append(value)
        original_close(value)
        raise OSError("simulated close failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(ts_builder.os, "close", close_then_raise)
        with pytest.raises(OSError, match="simulated close failure"):
            pinned.close()
        pinned.close()

    assert pinned.descriptor is None
    assert closed == [descriptor]


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock regression")
def test_transaction_lease_release_detaches_descriptor_before_close_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_file = tmp_path / "lock"
    descriptor = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    lease = ts_builder._TransactionLease(descriptor=descriptor, windows=False)
    original_close = os.close
    closed: list[int] = []

    def close_then_raise(value: int) -> None:
        closed.append(value)
        original_close(value)
        raise OSError("simulated close failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(ts_builder.os, "close", close_then_raise)
        with pytest.raises(OSError, match="simulated close failure"):
            lease.release()
        lease.release()

    assert lease.released is True
    assert closed == [descriptor]


def test_windows_directory_sync_uses_backup_semantics_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_calls: list[tuple[object, ...]] = []
    flushed: list[object] = []
    closed: list[object] = []

    def create_file(*args):
        create_calls.append(args)
        return 73

    monkeypatch.setattr(
        ts_builder,
        "_windows_directory_sync_calls",
        lambda: (
            create_file,
            lambda handle: flushed.append(handle) or True,
            lambda handle: closed.append(handle) or True,
            lambda: 0,
            -1,
        ),
    )

    ts_builder._fsync_directory_windows(tmp_path)

    assert create_calls == [
        (
            str(tmp_path),
            0x40000000,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x02000000,
            None,
        )
    ]
    assert flushed == [73]
    assert closed == [73]


def test_windows_pinned_handle_waits_for_namespace_guard_sharing_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handles = iter((-1, -1, 73))
    errors = iter((32, 33))
    sleeps: list[float] = []
    monkeypatch.setattr(ts_builder.time, "sleep", sleeps.append)

    handle = ts_builder._open_windows_handle_with_retry(
        lambda: next(handles),
        lambda: next(errors),
        -1,
        path=tmp_path,
        blocking=True,
    )

    assert handle == 73
    assert sleeps == [0.01, 0.01]


def test_windows_pinned_handle_nonblocking_sharing_conflict_fails_closed(
    tmp_path: Path,
) -> None:
    with pytest.raises(OSError, match="could not pin workspace path"):
        ts_builder._open_windows_handle_with_retry(
            lambda: -1,
            lambda: 32,
            -1,
            path=tmp_path,
            blocking=False,
        )


def test_windows_directory_sync_failure_is_required_and_closes_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[object] = []
    monkeypatch.setattr(
        ts_builder,
        "_windows_directory_sync_calls",
        lambda: (
            lambda *_args: 91,
            lambda _handle: False,
            lambda handle: closed.append(handle) or True,
            lambda: 5,
            -1,
        ),
    )

    with pytest.raises(OSError, match="FlushFileBuffers could not sync directory"):
        ts_builder._fsync_directory_windows(tmp_path)

    assert closed == [91]


def test_rollback_fsyncs_new_artifact_deletion_before_manifest_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_directory = tmp_path / "out"
    output_directory.mkdir()
    first = output_directory / "a.ts"
    second = output_directory / "b.ts"
    second.write_text("old-b\n", encoding="utf-8")
    original_replace = os.replace
    sync_events: list[tuple[str, Path]] = []

    def fail_second(source, destination, *args, **kwargs) -> None:
        if destination == second.name:
            raise OSError("simulated second replacement failure")
        original_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr("jaunt.typescript.builder.os.replace", fail_second)
    monkeypatch.setattr(
        ts_builder._PinnedDirectory,
        "fsync_required",
        lambda directory: sync_events.append(("required", directory.path)),
    )

    with pytest.raises(OSError, match="simulated second replacement failure"):
        atomic_write_manifest(
            tmp_path,
            (
                _Write("out/a.ts", "new-a\n", "implementation", "ts:a"),
                _Write("out/b.ts", "new-b\n", "implementation", "ts:b"),
            ),
        )

    transaction_directory = tmp_path / ".jaunt" / "transactions"
    assert not first.exists()
    assert second.read_text(encoding="utf-8") == "old-b\n"
    assert sync_events == [
        ("required", tmp_path),
        ("required", tmp_path),
        ("required", tmp_path / ".jaunt"),
        ("required", transaction_directory),
        ("required", output_directory),
        ("required", output_directory),
        ("required", transaction_directory),
    ]
    assert not tuple(transaction_directory.glob("*.json"))


def test_rollback_retains_manifest_when_new_artifact_deletion_cannot_be_synced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_directory = tmp_path / "out"
    output_directory.mkdir()
    first = output_directory / "a.ts"
    second = output_directory / "b.ts"
    second.write_text("old-b\n", encoding="utf-8")
    original_replace = os.replace
    original_fsync = os.fsync
    rollback_started = False

    def fail_second(source, destination, *args, **kwargs) -> None:
        nonlocal rollback_started
        if destination == second.name:
            rollback_started = True
            raise OSError("simulated second replacement failure")
        original_replace(source, destination, *args, **kwargs)

    def fail_rollback_sync(descriptor: int) -> None:
        if rollback_started:
            assert not first.exists()
            raise OSError("simulated rollback directory fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr("jaunt.typescript.builder.os.replace", fail_second)
    monkeypatch.setattr("jaunt.typescript.builder.os.fsync", fail_rollback_sync)

    with pytest.raises(OSError, match="simulated second replacement failure"):
        atomic_write_manifest(
            tmp_path,
            (
                _Write("out/a.ts", "new-a\n", "implementation", "ts:a"),
                _Write("out/b.ts", "new-b\n", "implementation", "ts:b"),
            ),
        )

    transaction_directory = tmp_path / ".jaunt" / "transactions"
    manifests = tuple(transaction_directory.glob("*.json"))
    assert not first.exists()
    assert second.read_text(encoding="utf-8") == "old-b\n"
    assert len(manifests) == 1


def test_forward_write_sync_failure_rolls_back_before_manifest_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_directory = tmp_path / "out"
    output_directory.mkdir()
    output = output_directory / "battery.test.ts"
    output.write_text("old\n", encoding="utf-8")
    output_syncs = 0

    def fail_first_output_sync(directory: ts_builder._PinnedDirectory) -> None:
        nonlocal output_syncs
        if directory.path != output_directory:
            return
        output_syncs += 1
        if output_syncs == 1:
            raise OSError("simulated forward write directory sync failure")

    monkeypatch.setattr(ts_builder._PinnedDirectory, "fsync_required", fail_first_output_sync)

    with pytest.raises(OSError, match="simulated forward write directory sync failure"):
        atomic_write_manifest(
            tmp_path,
            (_Write("out/battery.test.ts", "new\n", "test", "ts-test:battery"),),
        )

    assert output_syncs == 2
    assert output.read_text(encoding="utf-8") == "old\n"
    assert tuple((tmp_path / ".jaunt/transactions").glob("*.json")) == ()
    assert tuple(output_directory.glob("*.rollback-*")) == ()


def test_forward_deletion_sync_failure_restores_bytes_and_retains_prepared_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_directory = tmp_path / "out"
    output_directory.mkdir()
    output = output_directory / "obsolete.ts"
    output.write_text("old\n", encoding="utf-8")
    output_syncs = 0

    def fail_output_sync(directory: ts_builder._PinnedDirectory) -> None:
        nonlocal output_syncs
        if directory.path == output_directory:
            output_syncs += 1
            raise OSError("simulated deletion directory sync failure")

    monkeypatch.setattr(ts_builder._PinnedDirectory, "fsync_required", fail_output_sync)

    with pytest.raises(OSError, match="simulated deletion directory sync failure"):
        atomic_write_manifest(
            tmp_path,
            (_Write("out/obsolete.ts", None, "test", "ts-test:obsolete"),),
        )

    manifests = tuple((tmp_path / ".jaunt/transactions").glob("*.json"))
    assert output_syncs == 2
    assert output.read_text(encoding="utf-8") == "old\n"
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text(encoding="utf-8"))["state"] == "prepared"
    assert tuple(output_directory.glob("*.rollback-*")) == ()


@pytest.mark.parametrize(
    "marker_name",
    ("ts-pending.json", "test-repair-pending.json", "legacy.json"),
)
def test_atomic_publication_blocks_every_transaction_json(
    tmp_path: Path,
    marker_name: str,
) -> None:
    output = tmp_path / "out/value.ts"
    output.parent.mkdir()
    output.write_text("old\n", encoding="utf-8")
    manifest = _unresolved_transaction_marker(tmp_path, marker_name)
    assert ts_builder._has_pending_atomic_write_manifests(tmp_path)

    with pytest.raises(JauntGenerationError, match=marker_name):
        atomic_write_manifest(
            tmp_path,
            (_Write("out/value.ts", "new\n", "implementation", "ts:value"),),
        )

    assert output.read_text(encoding="utf-8") == "old\n"
    assert manifest.is_file()
    assert tuple(manifest.parent.glob("*.json")) == (manifest,)


def test_atomic_publication_allowlist_is_exact_and_keeps_foreign_markers_blocking(
    tmp_path: Path,
) -> None:
    output = tmp_path / "out/value.ts"
    output.parent.mkdir()
    output.write_text("old\n", encoding="utf-8")
    owned = _unresolved_transaction_marker(tmp_path, "design-owned.json")
    foreign = _unresolved_transaction_marker(tmp_path, "legacy.json")

    with pytest.raises(JauntGenerationError, match="legacy.json"):
        atomic_write_manifest(
            tmp_path,
            (_Write("out/value.ts", "new\n", "implementation", "ts:value"),),
            allowed_transaction_manifests=(owned.name,),
        )

    assert output.read_text(encoding="utf-8") == "old\n"
    foreign.unlink()
    atomic_write_manifest(
        tmp_path,
        (_Write("out/value.ts", "new\n", "implementation", "ts:value"),),
        allowed_transaction_manifests=(owned.name,),
    )
    assert output.read_text(encoding="utf-8") == "new\n"
    assert owned.is_file()


def test_atomic_publication_rejects_a_missing_allowlisted_marker(tmp_path: Path) -> None:
    output = tmp_path / "out/value.ts"
    output.parent.mkdir()
    output.write_text("old\n", encoding="utf-8")

    with pytest.raises(JauntGenerationError, match="design-missing.json"):
        atomic_write_manifest(
            tmp_path,
            (_Write("out/value.ts", "new\n", "implementation", "ts:value"),),
            allowed_transaction_manifests=("design-missing.json",),
        )

    assert output.read_text(encoding="utf-8") == "old\n"
    assert not tuple((tmp_path / ".jaunt/transactions").glob("*.json"))


@pytest.mark.parametrize(
    "marker_name",
    ("ts-pending.json", "test-repair-pending.json", "legacy.json"),
)
def test_current_artifact_proof_blocks_every_transaction_json(
    tmp_path: Path,
    marker_name: str,
) -> None:
    manifest = _unresolved_transaction_marker(tmp_path, marker_name)

    with pytest.raises(JauntGenerationError, match=marker_name):
        ts_tester._current_target_artifact_snapshot(tmp_path, (), strict=True)

    assert ts_tester._current_target_artifact_snapshot(tmp_path, (), strict=False) == (
        frozenset(),
        {},
    )
    assert manifest.is_file()


def test_status_recovers_committed_battery_transaction(tmp_path: Path) -> None:
    battery = tmp_path / "tests/__generated__/math.example.test.ts"
    battery.parent.mkdir(parents=True)
    battery.write_text("new battery\n", encoding="utf-8")
    manifest = _transaction_manifest(
        tmp_path,
        state="committed",
        writes=[
            {
                "path": battery.relative_to(tmp_path).as_posix(),
                "kind": "test",
                "moduleId": "ts-test:tests/math.jaunt.ts",
                "before": _digest("old battery\n"),
                "after": _digest("new battery\n"),
            }
        ],
    )

    status = classify_modules(tmp_path, ())

    assert not manifest.exists()
    assert battery.read_text(encoding="utf-8") == "new battery\n"
    assert all(item.code != "JAUNT_TS_INCOMPLETE_TRANSACTION" for item in status.diagnostics)


def test_recovery_clears_prepared_transaction_only_when_all_bytes_are_before(
    tmp_path: Path,
) -> None:
    battery = tmp_path / "tests/__generated__/math.example.test.ts"
    battery.parent.mkdir(parents=True)
    battery.write_text("old battery\n", encoding="utf-8")
    manifest = _transaction_manifest(
        tmp_path,
        state="prepared",
        writes=[
            {
                "path": battery.relative_to(tmp_path).as_posix(),
                "kind": "test",
                "moduleId": "ts-test:tests/math.jaunt.ts",
                "before": _digest("old battery\n"),
                "after": _digest("new battery\n"),
            }
        ],
    )

    assert _recover_atomic_write_manifests(tmp_path) == (manifest,)
    assert not manifest.exists()
    assert battery.read_text(encoding="utf-8") == "old battery\n"


def test_recovery_keeps_prepared_all_after_marker_without_final_seal(tmp_path: Path) -> None:
    battery = tmp_path / "tests/__generated__/math.example.test.ts"
    battery.parent.mkdir(parents=True)
    battery.write_text("new battery\n", encoding="utf-8")
    manifest = _transaction_manifest(
        tmp_path,
        state="prepared",
        writes=[
            {
                "path": battery.relative_to(tmp_path).as_posix(),
                "kind": "test",
                "moduleId": "ts-test:tests/math.jaunt.ts",
                "before": _digest("old battery\n"),
                "after": _digest("new battery\n"),
            }
        ],
    )

    status = classify_modules(tmp_path, ())

    assert manifest.exists()
    assert any(item.code == "JAUNT_TS_INCOMPLETE_TRANSACTION" for item in status.diagnostics)


def test_recovery_keeps_legacy_prepared_all_after_marker(tmp_path: Path) -> None:
    battery = tmp_path / "tests/__generated__/math.example.test.ts"
    battery.parent.mkdir(parents=True)
    battery.write_text("new battery\n", encoding="utf-8")
    manifest = _transaction_manifest(
        tmp_path,
        state="prepared",
        legacy=True,
        writes=[
            {
                "path": battery.relative_to(tmp_path).as_posix(),
                "kind": "test",
                "moduleId": "ts-test:tests/math.jaunt.ts",
                "before": _digest("old battery\n"),
                "after": _digest("new battery\n"),
            }
        ],
    )

    assert _recover_atomic_write_manifests(tmp_path) == ()
    assert manifest.exists()


@pytest.mark.parametrize(
    ("state", "current"),
    (("prepared", "old\n"), ("committed", "new\n")),
    ids=("prepared-all-before", "committed-all-after"),
)
def test_live_writer_lease_blocks_concurrent_recovery(
    tmp_path: Path,
    state: str,
    current: str,
) -> None:
    output = tmp_path / "out/battery.test.ts"
    output.parent.mkdir()
    output.write_text(current, encoding="utf-8")
    manifest = tmp_path / ".jaunt/transactions/ts-live-writer.json"
    ready = tmp_path / "writer-ready"
    release = tmp_path / "writer-release"
    payload = {
        "scheme": _TRANSACTION_SCHEME,
        "state": state,
        "writes": [
            {
                "path": "out/battery.test.ts",
                "kind": "test",
                "moduleId": "ts-test:battery",
                "before": _digest("old\n"),
                "after": _digest("new\n"),
            }
        ],
    }
    script = """
import json
import sys
import time
from pathlib import Path
from jaunt.typescript.builder import (
    _PinnedWorkspace,
    _acquire_transaction_lease,
    _write_transaction_manifest,
)

root = Path(sys.argv[1])
manifest = root / ".jaunt/transactions/ts-live-writer.json"
ready = root / "writer-ready"
release = root / "writer-release"
payload = json.loads(sys.argv[2])
with _PinnedWorkspace(root) as workspace:
    pinned_directory = workspace.directory(manifest.parent)
    lease = _acquire_transaction_lease(
        manifest.parent,
        blocking=True,
        pinned_directory=pinned_directory,
        authority_directory=workspace.root_directory,
    )
    assert lease is not None
    try:
        _write_transaction_manifest(manifest, payload, pinned_directory=pinned_directory)
        ready.write_text("ready\\n", encoding="utf-8")
        deadline = time.monotonic() + 10
        while not release.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("parent did not release transaction writer")
            time.sleep(0.01)
    finally:
        lease.release()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), json.dumps(payload)],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), f"transaction writer exited early with {process.poll()}"

        assert _recover_atomic_write_manifests(tmp_path) == ()
        assert manifest.exists()
    finally:
        release.write_text("release\n", encoding="utf-8")
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            pytest.fail(f"transaction writer did not exit: {stdout}\n{stderr}")
    assert process.returncode == 0, stderr

    assert _recover_atomic_write_manifests(tmp_path) == (manifest,)
    assert not manifest.exists()
    assert (manifest.parent / ".atomic-write.lock").is_file() is (os.name == "nt")


def test_repeated_successful_transactions_reuse_one_global_lock(tmp_path: Path) -> None:
    output = tmp_path / "out/value.ts"
    output.parent.mkdir()

    atomic_write_manifest(
        tmp_path,
        (_Write("out/value.ts", "one\n", "implementation", "ts:value"),),
    )
    atomic_write_manifest(
        tmp_path,
        (_Write("out/value.ts", "two\n", "implementation", "ts:value"),),
    )

    transaction_directory = tmp_path / ".jaunt/transactions"
    assert output.read_text(encoding="utf-8") == "two\n"
    assert tuple(transaction_directory.glob("*.json")) == ()
    assert tuple(path.name for path in transaction_directory.glob("*.lock")) == (
        (".atomic-write.lock",) if os.name == "nt" else ()
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX writers can snapshot before the root lease")
def test_global_lease_rechecks_output_cas_after_waiting_writer_acquires(
    tmp_path: Path,
) -> None:
    output = tmp_path / "out/value.ts"
    output.parent.mkdir()
    output.write_text("old\n", encoding="utf-8")
    first_guard = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()

    def hold_first_guard() -> None:
        first_guard.set()
        assert release_first.wait(5), "second writer never reached the lease"

    def write(name: str, content: str, *, hold: bool = False) -> None:
        if name == "second":
            second_started.set()
        atomic_write_manifest(
            tmp_path,
            (_Write("out/value.ts", content, "implementation", "ts:value"),),
            pre_commit_guard=hold_first_guard if hold else None,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(write, "first", "first\n", hold=True)
        assert first_guard.wait(5)
        second = pool.submit(write, "second", "second\n")
        assert second_started.wait(5)
        release_first.set()
        first.result(timeout=5)
        with pytest.raises(JauntGenerationError, match="artifact changed during validation"):
            second.result(timeout=5)

    assert output.read_text(encoding="utf-8") == "first\n"


@pytest.mark.skipif(os.name != "nt", reason="Windows root-handle serialization only")
def test_windows_root_pin_serializes_writers_before_their_snapshot(tmp_path: Path) -> None:
    output = tmp_path / "out/value.ts"
    output.parent.mkdir()
    output.write_text("old\n", encoding="utf-8")
    first_guard = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()

    def hold_first_guard() -> None:
        first_guard.set()
        assert release_first.wait(5), "second writer did not start"

    def write(name: str, content: str, *, hold: bool = False) -> None:
        if name == "second":
            second_started.set()
        atomic_write_manifest(
            tmp_path,
            (_Write("out/value.ts", content, "implementation", "ts:value"),),
            pre_commit_guard=hold_first_guard if hold else None,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(write, "first", "first\n", hold=True)
        assert first_guard.wait(5)
        second = pool.submit(write, "second", "second\n")
        assert second_started.wait(5)
        assert not second.done()
        release_first.set()
        first.result(timeout=5)
        second.result(timeout=5)

    assert output.read_text(encoding="utf-8") == "second\n"


@pytest.mark.skipif(os.name == "nt", reason="Windows pinned handles deny directory replacement")
def test_root_inode_lease_serializes_after_transaction_directory_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "out/value.ts"
    output.parent.mkdir()
    output.write_text("old\n", encoding="utf-8")
    first_guard = threading.Event()
    release_first = threading.Event()
    second_attempted_lock = threading.Event()
    role = threading.local()

    from jaunt.typescript import builder as builder_module

    original_acquire = builder_module._acquire_transaction_lease

    def observed_acquire(directory: Path, *, blocking: bool, **kwargs):
        if getattr(role, "name", None) == "second":
            second_attempted_lock.set()
        return original_acquire(directory, blocking=blocking, **kwargs)

    def hold_first_guard() -> None:
        first_guard.set()
        assert release_first.wait(5), "second writer never reached the root lease"

    def write(name: str, content: str, *, hold: bool = False) -> None:
        role.name = name
        atomic_write_manifest(
            tmp_path,
            (_Write("out/value.ts", content, "implementation", "ts:value"),),
            pre_commit_guard=hold_first_guard if hold else None,
        )

    monkeypatch.setattr(builder_module, "_acquire_transaction_lease", observed_acquire)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(write, "first", "first\n", hold=True)
        assert first_guard.wait(5)

        transaction_directory = tmp_path / ".jaunt/transactions"
        displaced_directory = tmp_path / ".jaunt/transactions-displaced"
        transaction_directory.rename(displaced_directory)
        transaction_directory.mkdir()

        second = pool.submit(write, "second", "second\n")
        assert second_attempted_lock.wait(5)
        with pytest.raises(TimeoutError):
            second.result(timeout=0.1)

        release_first.set()
        with pytest.raises(JauntGenerationError, match="no longer bound"):
            first.result(timeout=5)
        second.result(timeout=5)

    assert output.read_text(encoding="utf-8") == "second\n"


def test_atomic_write_publishes_committed_state_only_after_final_seal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "out/battery.test.ts"
    output.parent.mkdir()
    output.write_text("old\n", encoding="utf-8")
    observed_states: list[str] = []

    def seal() -> None:
        manifest = next((tmp_path / ".jaunt/transactions").glob("ts-*.json"))
        observed_states.append(json.loads(manifest.read_text(encoding="utf-8"))["state"])
        assert output.read_text(encoding="utf-8") == "new\n"

    def retain(_manifest: Path, payload: dict[str, Any], **_kwargs) -> bool:
        observed_states.append(str(payload["state"]))
        return False

    with monkeypatch.context() as patcher:
        patcher.setattr("jaunt.typescript.builder._retire_transaction_manifest", retain)
        with pytest.raises(JauntGenerationError, match="could not be durably retired"):
            atomic_write_manifest(
                tmp_path,
                (_Write("out/battery.test.ts", "new\n", "test", "ts-test:spec"),),
                commit_seal=seal,
            )

    manifest = next((tmp_path / ".jaunt/transactions").glob("ts-*.json"))
    assert observed_states == ["prepared", "committed"]
    assert json.loads(manifest.read_text(encoding="utf-8"))["state"] == "committed"
    assert _recover_atomic_write_manifests(tmp_path) == (manifest,)
    assert output.read_text(encoding="utf-8") == "new\n"


def test_committed_manifest_publish_failure_rolls_outputs_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "out/battery.test.ts"
    output.parent.mkdir()
    output.write_text("old\n", encoding="utf-8")

    from jaunt.typescript import builder as builder_module

    original_write_manifest = builder_module._write_transaction_manifest
    states: list[str] = []

    def fail_after_committed_publish(manifest: Path, payload: dict[str, Any], **kwargs) -> None:
        original_write_manifest(manifest, payload, **kwargs)
        state = str(payload["state"])
        states.append(state)
        if state == "committed":
            raise OSError("simulated committed marker fsync failure")

    monkeypatch.setattr(
        builder_module,
        "_write_transaction_manifest",
        fail_after_committed_publish,
    )

    with pytest.raises(OSError, match="simulated committed marker fsync failure"):
        atomic_write_manifest(
            tmp_path,
            (_Write("out/battery.test.ts", "new\n", "test", "ts-test:spec"),),
        )

    assert states == ["prepared", "committed"]
    assert output.read_text(encoding="utf-8") == "old\n"
    assert tuple((tmp_path / ".jaunt/transactions").glob("ts-*.json")) == ()


def test_recovery_keeps_mixed_transaction_blocking(tmp_path: Path) -> None:
    first = tmp_path / "out/a.ts"
    second = tmp_path / "out/b.ts"
    first.parent.mkdir()
    first.write_text("new-a\n", encoding="utf-8")
    second.write_text("old-b\n", encoding="utf-8")
    manifest = _transaction_manifest(
        tmp_path,
        state="committed",
        writes=[
            {
                "path": "out/a.ts",
                "kind": "test",
                "moduleId": "ts-test:a",
                "before": _digest("old-a\n"),
                "after": _digest("new-a\n"),
            },
            {
                "path": "out/b.ts",
                "kind": "test",
                "moduleId": "ts-test:b",
                "before": _digest("old-b\n"),
                "after": _digest("new-b\n"),
            },
        ],
    )

    assert _recover_atomic_write_manifests(tmp_path) == ()
    assert manifest.exists()


@pytest.mark.parametrize(
    "write",
    [
        {
            "path": "../outside.ts",
            "kind": "test",
            "moduleId": "ts-test:outside",
            "before": MISSING_INPUT,
            "after": _digest("new\n"),
        },
        {
            "path": "out/a.ts",
            "kind": "test",
            "moduleId": "ts-test:a",
            "before": "not-a-digest",
            "after": _digest("new\n"),
        },
    ],
    ids=("external-path", "malformed-hash"),
)
def test_recovery_keeps_unsafe_or_malformed_transaction(
    tmp_path: Path, write: dict[str, Any]
) -> None:
    manifest = _transaction_manifest(tmp_path, state="prepared", writes=[write])

    assert _recover_atomic_write_manifests(tmp_path) == ()
    assert manifest.exists()


def test_recovery_understands_committed_deletion_sentinel(tmp_path: Path) -> None:
    manifest = _transaction_manifest(
        tmp_path,
        state="committed",
        writes=[
            {
                "path": "out/deleted.ts",
                "kind": "delete",
                "moduleId": "ts-test:deleted",
                "before": _digest("old\n"),
                "after": MISSING_INPUT,
            }
        ],
    )

    assert _recover_atomic_write_manifests(tmp_path) == (manifest,)
    assert not manifest.exists()


def test_recovery_never_interprets_design_manifest_as_byte_commit(tmp_path: Path) -> None:
    source = tmp_path / "src/math.jaunt.ts"
    source.parent.mkdir()
    source.write_text("new declaration\n", encoding="utf-8")
    manifest = _transaction_manifest(
        tmp_path,
        name="design-crash.json",
        state="committed",
        writes=[
            {
                "path": source.relative_to(tmp_path).as_posix(),
                "kind": "design",
                "moduleId": "ts:src/math",
                "before": _digest("old declaration\n"),
                "after": _digest("new declaration\n"),
            }
        ],
    )

    status = classify_modules(tmp_path, ())

    assert manifest.exists()
    assert any(item.code == "JAUNT_TS_INCOMPLETE_TRANSACTION" for item in status.diagnostics)


def test_recovery_restores_marker_when_transaction_directory_fsync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    battery = tmp_path / "out/battery.test.ts"
    battery.parent.mkdir()
    battery.write_text("new\n", encoding="utf-8")
    manifest = _transaction_manifest(
        tmp_path,
        state="committed",
        writes=[
            {
                "path": "out/battery.test.ts",
                "kind": "test",
                "moduleId": "ts-test:battery",
                "before": _digest("old\n"),
                "after": _digest("new\n"),
            }
        ],
    )

    original_sync = ts_builder._PinnedDirectory.fsync_required

    def fail_transaction_directory_sync(directory: ts_builder._PinnedDirectory) -> None:
        if directory.path == manifest.parent:
            raise OSError("simulated fsync failure")
        original_sync(directory)

    with monkeypatch.context() as patcher:
        patcher.setattr(
            ts_builder._PinnedDirectory,
            "fsync_required",
            fail_transaction_directory_sync,
        )
        assert _recover_atomic_write_manifests(tmp_path) == ()
        assert manifest.exists()
        assert json.loads(manifest.read_text(encoding="utf-8"))["state"] == "committed"

    assert _recover_atomic_write_manifests(tmp_path) == (manifest,)


@pytest.mark.skipif(os.name == "nt", reason="Windows directory handles deny the rename itself")
def test_recovery_refuses_to_retire_marker_after_transaction_directory_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    battery = tmp_path / "out/battery.test.ts"
    battery.parent.mkdir()
    battery.write_text("new\n", encoding="utf-8")
    manifest = _transaction_manifest(
        tmp_path,
        state="committed",
        writes=[
            {
                "path": "out/battery.test.ts",
                "kind": "test",
                "moduleId": "ts-test:battery",
                "before": _digest("old\n"),
                "after": _digest("new\n"),
            }
        ],
    )
    transaction_directory = manifest.parent
    pinned_location = transaction_directory.with_name("transactions-pinned")
    outside = tmp_path.parent / f"{tmp_path.name}-recovery-outside"
    outside.mkdir()
    outsider = outside / "ts-outsider.json"
    outsider.write_text("outside\n", encoding="utf-8")
    original_iter_names = ts_builder._PinnedDirectory.iter_names
    swapped = False

    def swap_before_listing(directory: ts_builder._PinnedDirectory, pattern: str):
        nonlocal swapped
        if directory.path == transaction_directory and not swapped:
            transaction_directory.rename(pinned_location)
            transaction_directory.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_iter_names(directory, pattern)

    monkeypatch.setattr(ts_builder._PinnedDirectory, "iter_names", swap_before_listing)
    try:
        assert _recover_atomic_write_manifests(tmp_path) == ()
        assert swapped is True
        assert (pinned_location / manifest.name).exists()
        assert outsider.read_text(encoding="utf-8") == "outside\n"
    finally:
        transaction_directory.unlink(missing_ok=True)
        outsider.unlink(missing_ok=True)
        outside.rmdir()
