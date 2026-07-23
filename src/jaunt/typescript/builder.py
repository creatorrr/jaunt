"""High-level TypeScript discovery, synchronization, and build transactions.

The Node worker is the semantic authority.  This module deliberately treats every
worker response as a proposal: artifacts are written only after ``validateOverlay``
returns exact bytes and the workspace inputs still match the analyzed snapshot.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fnmatch
import hashlib
import inspect
import json
import os
import posixpath
import re
import stat
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any, Protocol, TypeAlias, cast

import jaunt
from jaunt.cache import ResponseCache
from jaunt.config import JauntConfig
from jaunt.cost import CostTracker
from jaunt.errors import JauntConfigError, JauntGenerationError
from jaunt.generate.base import GenerationRequest, GenerationResult, GeneratorBackend, TokenUsage
from jaunt.generate.codex_backend import CodexBackend
from jaunt.generate.codex_backend import run_codex_exec
from jaunt.generate.request_cache import generate_request_cached, store_generation_result
from jaunt.journal import JournalEvent, append_events
from jaunt.skill_seed import skills_fingerprint
from jaunt.targets.base import TargetBuildReport, TargetDiagnostic
from jaunt.typescript.config import TypeScriptTargetConfig
from jaunt.typescript.protocol import (
    InitializeParams,
    InitializeResult,
    ProtocolDiagnostic,
    ValidateOverlayParams,
    ValidateOverlayResult,
)
from jaunt.typescript.reuse import capture_target_api_records, update_target_api_reuse_proof
from jaunt.typescript.worker import (
    WorkerClient,
    resolve_worker_installation,
    validate_worker_capabilities,
)

_DEFAULT_ATTEMPTS = 3
_ANALYZE_CONTRACT_BATCH_SIZE = 4
_SYNC_BATCH_SIZE = 4
_PLACEHOLDER_MARKERS = ("state=unbuilt", 'state = "unbuilt"', "state: unbuilt")
MISSING_INPUT = "<missing>"
_WINDOWS_RESERVED_LEAF_BASES = frozenset(
    {"aux", "clock$", "con", "conin$", "conout$", "nul", "prn"}
    | {
        f"{prefix}{suffix}"
        for prefix in ("com", "lpt")
        for suffix in ("1", "2", "3", "4", "5", "6", "7", "8", "9", "¹", "²", "³")
    }
)
_IMPORTED_TYPE_CONTEXT_BEGIN = "// <jaunt:imported-type-context version=2 encoding=base64-json>"
_LEGACY_IMPORTED_TYPE_CONTEXT_BEGIN = "// <jaunt:imported-type-context version=1>"
_IMPORTED_TYPE_CONTEXT_END = "// </jaunt:imported-type-context>"


class _CommittedBatteryInfrastructureError(RuntimeError):
    """Stop candidate retries when the protected battery runner itself is unavailable."""

    def __init__(
        self,
        errors: Sequence[str],
        *,
        candidate_source: str | None = None,
    ) -> None:
        self.errors = tuple(errors)
        self.candidate_source = candidate_source
        super().__init__(self.errors[0] if self.errors else "Committed battery validation failed")

    def attach_candidate(self, source: str) -> _CommittedBatteryInfrastructureError:
        """Preserve the conformance-valid bytes that reached the unavailable runner."""

        if self.candidate_source is None:
            self.candidate_source = source
        return self


def _unsafe_portable_leaf(name: str) -> bool:
    """Return whether one generated path component is unsafe on supported hosts."""

    windows_base = name.split(".", 1)[0].rstrip(" .").casefold()
    return (
        not name
        or name in {".", ".."}
        or any(character in name for character in ("\0", "/", "\\", ":"))
        or name.endswith((".", " "))
        or windows_base in _WINDOWS_RESERVED_LEAF_BASES
        or any(ord(character) < 32 or character in '<>"|?*' for character in name)
    )


class WorkerLike(Protocol):
    """Small worker surface used by the high-level operations and test fakes."""

    installation: Any

    async def initialize(self, params: InitializeParams) -> InitializeResult: ...

    async def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]: ...


WorkerFactory: TypeAlias = Callable[
    [Path, TypeScriptTargetConfig],
    WorkerLike | Awaitable[WorkerLike],
]


def _verify_worker_runtime_identity(client: WorkerLike) -> None:
    """Recheck a real worker's content pin without burdening protocol-only fakes."""

    verify = getattr(client, "verify_runtime_identity", None)
    if callable(verify):
        verify()


def _seal_worker_runtime_identity(client: WorkerLike) -> None:
    """Verify and seal a real worker at the final rollback-capable boundary."""

    seal = getattr(client, "seal_runtime_identity", None)
    if callable(seal):
        seal()
    else:
        _verify_worker_runtime_identity(client)


@dataclass(frozen=True, slots=True)
class TypeScriptAnalysis:
    """One worker snapshot shared by a single high-level operation."""

    initialized: InitializeResult
    workspace: Mapping[str, Any]
    contracts: Mapping[str, Any]

    @property
    def modules(self) -> tuple[Mapping[str, Any], ...]:
        value = self.contracts.get("modules", [])
        return (
            tuple(item for item in value if isinstance(item, Mapping))
            if isinstance(value, list)
            else ()
        )


@dataclass(frozen=True, slots=True)
class SyncReport:
    mirrors: tuple[str, ...] = ()
    placeholders: tuple[str, ...] = ()
    created_facades: tuple[str, ...] = ()
    failed: Mapping[str, tuple[TargetDiagnostic, ...]] = field(default_factory=dict)
    exit_code: int = 0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True, slots=True)
class _Write:
    path: str
    content: str | None
    kind: str
    module_id: str


@dataclass(frozen=True, slots=True)
class _BuildUnit:
    """An owner/reference component committed as one artifact transaction."""

    key: str
    module_ids: tuple[str, ...]


def _target(config: JauntConfig) -> TypeScriptTargetConfig:
    target = config.typescript_target
    if target is None:
        raise JauntConfigError("No [target.ts] is configured in jaunt.toml")
    return target


def _safe_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or any(_unsafe_portable_leaf(part) for part in path.parts):
        raise JauntConfigError(f"TypeScript worker returned an unsafe path: {relative!r}")
    resolved_root = root.resolve()
    resolved = (resolved_root / path).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise JauntConfigError(f"TypeScript worker path escapes the workspace: {relative!r}")
    return resolved


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _path_hash(path: Path) -> str | None:
    try:
        return _sha256(path.read_bytes())
    except FileNotFoundError:
        return None


def _windows_directory_sync_calls() -> tuple[
    Callable[..., object],
    Callable[[object], object],
    Callable[[object], object],
    Callable[[], int],
    object,
]:
    """Return typed Win32 calls used to flush one directory handle."""

    import ctypes
    from ctypes import wintypes

    win_dll = getattr(ctypes, "WinDLL", None)
    get_last_error = getattr(ctypes, "get_last_error", None)
    if not callable(win_dll) or not callable(get_last_error):
        raise OSError(errno.ENOSYS, "Win32 durability APIs are unavailable")
    kernel32 = win_dll("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    flush_file_buffers = kernel32.FlushFileBuffers
    flush_file_buffers.argtypes = [wintypes.HANDLE]
    flush_file_buffers.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    return (
        create_file,
        flush_file_buffers,
        close_handle,
        cast("Callable[[], int]", get_last_error),
        wintypes.HANDLE(-1).value,
    )


def _fsync_directory_windows(path: Path) -> None:
    """Flush a Windows directory through a backup-semantics Win32 handle."""

    create_file, flush_file_buffers, close_handle, get_last_error, invalid_handle = (
        _windows_directory_sync_calls()
    )
    generic_write = 0x40000000
    share_read_write_delete = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    handle = create_file(
        str(path),
        generic_write,
        share_read_write_delete,
        None,
        open_existing,
        file_flag_backup_semantics,
        None,
    )
    if handle == invalid_handle:
        error = get_last_error()
        raise OSError(error, "CreateFileW could not open directory for durability sync", str(path))
    try:
        if not flush_file_buffers(handle):
            error = get_last_error()
            raise OSError(error, "FlushFileBuffers could not sync directory", str(path))
    finally:
        closed = bool(close_handle(handle))
    if not closed:
        error = get_last_error()
        raise OSError(error, "CloseHandle failed after directory durability sync", str(path))


def _fsync_directory(path: Path) -> None:
    """Persist directory-entry updates where the filesystem supports it."""

    if os.name == "nt":
        try:
            _fsync_directory_windows(path)
        except OSError:
            pass
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _fsync_directory_required(path: Path) -> None:
    """Confirm a rollback directory update, raising when durability is unknown."""

    if os.name == "nt":
        _fsync_directory_windows(path)
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_windows_pinned_attributes(
    path: Path,
    file_attributes: int,
    *,
    directory: bool,
) -> None:
    file_attribute_directory = 0x00000010
    file_attribute_reparse_point = 0x00000400
    if file_attributes & file_attribute_reparse_point:
        raise JauntGenerationError(f"Refusing to pin a redirecting workspace entry: {path}")
    is_directory = bool(file_attributes & file_attribute_directory)
    if directory != is_directory:
        error_type = NotADirectoryError if directory else IsADirectoryError
        raise error_type(path)


def _open_windows_handle_with_retry(
    opener: Callable[[], object],
    get_last_error: Callable[[], int],
    invalid_handle: object,
    *,
    path: Path,
    blocking: bool,
) -> object:
    while True:
        handle = opener()
        if handle != invalid_handle:
            return handle
        error = get_last_error()
        if blocking and error in {32, 33}:  # sharing/lock violation
            time.sleep(0.01)
            continue
        if error in {2, 3}:
            raise FileNotFoundError(error, "Pinned path does not exist", str(path))
        raise OSError(error, "CreateFileW could not pin workspace path", str(path))


def _windows_open_pinned_path(
    path: Path,
    *,
    directory: bool,
    create_file: bool = False,
    writable: bool = True,
    share_write: bool = False,
    blocking: bool = True,
) -> int:
    """Open one Windows entry without following a reparse point.

    Directory handles deliberately omit ``FILE_SHARE_DELETE``. Keeping the root
    and every descendant handle alive therefore prevents a junction/symlink swap
    while path-based Win32 publication is in progress.
    """

    import ctypes
    from ctypes import wintypes

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]

    win_dll = getattr(ctypes, "WinDLL", None)
    get_last_error = getattr(ctypes, "get_last_error", None)
    if not callable(win_dll) or not callable(get_last_error):
        raise OSError(errno.ENOSYS, "Win32 pinned-directory APIs are unavailable")
    kernel32 = win_dll("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    get_info.restype = wintypes.BOOL
    close = kernel32.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL

    generic_read = 0x80000000
    generic_write = 0x40000000
    share_read = 0x00000001
    file_share_write = 0x00000002
    open_existing = 3
    open_always = 4
    backup_semantics = 0x02000000
    open_reparse_point = 0x00200000
    invalid_handle = wintypes.HANDLE(-1).value
    handle = _open_windows_handle_with_retry(
        lambda: create(
            str(path),
            generic_read | (generic_write if writable else 0),
            share_read | (file_share_write if share_write else 0),
            None,
            open_always if create_file else open_existing,
            open_reparse_point | (backup_semantics if directory else 0),
            None,
        ),
        cast("Callable[[], int]", get_last_error),
        invalid_handle,
        path=path,
        blocking=blocking,
    )
    try:
        info = FileAttributeTagInfo()
        file_attribute_tag_info = 9
        if not get_info(handle, file_attribute_tag_info, ctypes.byref(info), ctypes.sizeof(info)):
            error = get_last_error()
            raise OSError(error, "Could not inspect pinned workspace path", str(path))
        _validate_windows_pinned_attributes(
            path,
            int(info.FileAttributes),
            directory=directory,
        )
    except BaseException:
        close(handle)
        raise
    return cast(int, handle)


def _windows_close_pinned_handle(handle: object) -> None:
    _create, _flush, close, get_last_error, _invalid = _windows_directory_sync_calls()
    if not close(handle):
        error = get_last_error()
        raise OSError(error, "CloseHandle failed for pinned workspace directory")


def _windows_flush_pinned_handle(handle: object, path: Path) -> None:
    _create, flush, _close, get_last_error, _invalid = _windows_directory_sync_calls()
    if not flush(handle):
        error = get_last_error()
        raise OSError(error, "FlushFileBuffers could not sync directory", str(path))


@dataclass(slots=True)
class _PinnedDirectory:
    """One directory pinned against redirect traversal for a transaction."""

    path: Path
    descriptor: int | None = None
    windows_handle: object | None = None
    blocking: bool = True

    @staticmethod
    def _leaf(name: str) -> str:
        if _unsafe_portable_leaf(name):
            raise JauntGenerationError(f"Unsafe pinned-directory leaf name: {name!r}")
        return name

    def close(self) -> None:
        first_error: OSError | None = None
        if self.descriptor is not None:
            descriptor = self.descriptor
            self.descriptor = None
            try:
                os.close(descriptor)
            except OSError as error:
                first_error = error
        if self.windows_handle is not None:
            handle = self.windows_handle
            self.windows_handle = None
            try:
                _windows_close_pinned_handle(handle)
            except OSError as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    def fsync_required(self) -> None:
        if self.windows_handle is not None:
            _windows_flush_pinned_handle(self.windows_handle, self.path)
            return
        if self.descriptor is None:  # pragma: no cover - defensive
            raise RuntimeError("Pinned directory is closed")
        os.fsync(self.descriptor)

    def _open_regular_read(self, name: str) -> int:
        leaf = self._leaf(name)
        if self.windows_handle is not None:
            import msvcrt

            open_osfhandle = cast(
                "Callable[[int, int], int]",
                getattr(msvcrt, "open_osfhandle", None),
            )
            if not callable(open_osfhandle):  # pragma: no cover - Windows runtime invariant
                raise OSError(errno.ENOSYS, "msvcrt.open_osfhandle is unavailable")
            handle = _windows_open_pinned_path(
                self.path / leaf,
                directory=False,
                writable=False,
                blocking=self.blocking,
            )
            try:
                return open_osfhandle(
                    handle,
                    os.O_RDONLY | getattr(os, "O_BINARY", 0),
                )
            except BaseException:
                _windows_close_pinned_handle(handle)
                raise
        if self.descriptor is None:  # pragma: no cover - defensive
            raise RuntimeError("Pinned directory is closed")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        return os.open(leaf, flags, dir_fd=self.descriptor)

    def read_bytes_with_stat(self, name: str) -> tuple[bytes, os.stat_result]:
        descriptor = self._open_regular_read(name)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise IsADirectoryError(self.path / name)
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                content = stream.read()
            return content, metadata
        finally:
            os.close(descriptor)

    def read_bytes(self, name: str) -> bytes:
        return self.read_bytes_with_stat(name)[0]

    def stat(self, name: str) -> os.stat_result | None:
        try:
            descriptor = self._open_regular_read(name)
        except FileNotFoundError:
            return None
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise IsADirectoryError(self.path / name)
            return metadata
        finally:
            os.close(descriptor)

    def path_hash(self, name: str) -> str | None:
        try:
            return _sha256(self.read_bytes(name))
        except FileNotFoundError:
            return None

    def iter_names(self, pattern: str) -> tuple[str, ...]:
        if not pattern or any(character in pattern for character in ("\0", "/", "\\", ":")):
            raise JauntGenerationError(f"Unsafe pinned-directory match pattern: {pattern!r}")
        if self.windows_handle is not None:
            names = os.listdir(self.path)
        else:
            if self.descriptor is None:  # pragma: no cover - defensive
                raise RuntimeError("Pinned directory is closed")
            names = os.listdir(self.descriptor)
        matched = [name for name in names if fnmatch.fnmatchcase(name, pattern)]
        for name in matched:
            self._leaf(name)
        return tuple(sorted(matched))

    def create_temp(self, prefix: str, suffix: str = "") -> tuple[int, str]:
        for _attempt in range(128):
            leaf = self._leaf(f"{prefix}{uuid.uuid4().hex}{suffix}")
            flags = (
                os.O_CREAT
                | os.O_EXCL
                | os.O_RDWR
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                if self.windows_handle is not None:
                    return os.open(self.path / leaf, flags, 0o600), leaf
                if self.descriptor is None:  # pragma: no cover - defensive
                    raise RuntimeError("Pinned directory is closed")
                return os.open(leaf, flags, 0o600, dir_fd=self.descriptor), leaf
            except FileExistsError:
                continue
        raise FileExistsError(errno.EEXIST, "Could not reserve a unique transaction temp name")

    def open_lock(self, name: str, flags: int, mode: int, *, blocking: bool) -> int:
        leaf = self._leaf(name)
        if self.windows_handle is not None:
            import msvcrt

            open_osfhandle = cast(
                "Callable[[int, int], int]",
                getattr(msvcrt, "open_osfhandle", None),
            )
            if not callable(open_osfhandle):  # pragma: no cover - Windows runtime invariant
                raise OSError(errno.ENOSYS, "msvcrt.open_osfhandle is unavailable")
            handle = _windows_open_pinned_path(
                self.path / leaf,
                directory=False,
                create_file=bool(flags & os.O_CREAT),
                share_write=True,
                blocking=blocking,
            )
            try:
                return open_osfhandle(
                    handle,
                    os.O_RDWR | getattr(os, "O_BINARY", 0),
                )
            except BaseException:
                _windows_close_pinned_handle(handle)
                raise
        if self.descriptor is None:  # pragma: no cover - defensive
            raise RuntimeError("Pinned directory is closed")
        return os.open(
            leaf,
            flags | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=self.descriptor,
        )

    def replace(self, source: str, destination: str) -> None:
        source_leaf = self._leaf(source)
        destination_leaf = self._leaf(destination)
        if self.windows_handle is not None:
            os.replace(self.path / source_leaf, self.path / destination_leaf)
            return
        if self.descriptor is None:  # pragma: no cover - defensive
            raise RuntimeError("Pinned directory is closed")
        os.replace(
            source_leaf,
            destination_leaf,
            src_dir_fd=self.descriptor,
            dst_dir_fd=self.descriptor,
        )

    def unlink(self, name: str, *, missing_ok: bool = False) -> bool:
        leaf = self._leaf(name)
        try:
            if self.windows_handle is not None:
                os.unlink(self.path / leaf)
            else:
                if self.descriptor is None:  # pragma: no cover - defensive
                    raise RuntimeError("Pinned directory is closed")
                os.unlink(leaf, dir_fd=self.descriptor)
        except FileNotFoundError:
            if not missing_ok:
                raise
            return False
        return True


class _PinnedWorkspace:
    """Hold a no-follow directory chain for one publication transaction."""

    def __init__(self, root: Path, *, blocking: bool = True) -> None:
        self.root = Path(os.path.abspath(root))
        self.blocking = blocking
        self.created_directories: list[Path] = []
        if os.name == "nt":
            root_directory = _PinnedDirectory(
                path=self.root,
                windows_handle=_windows_open_pinned_path(
                    self.root,
                    directory=True,
                    blocking=blocking,
                ),
                blocking=blocking,
            )
        else:
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            root_directory = _PinnedDirectory(
                path=self.root,
                descriptor=os.open(self.root, flags),
                blocking=blocking,
            )
        self._directories: dict[Path, _PinnedDirectory] = {self.root: root_directory}

    def __enter__(self) -> _PinnedWorkspace:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @property
    def root_directory(self) -> _PinnedDirectory:
        """Return the pinned workspace root while this workspace is open."""

        try:
            return self._directories[self.root]
        except KeyError as error:
            raise RuntimeError("Pinned workspace is closed") from error

    def close(self) -> None:
        first_error: OSError | None = None
        for directory in reversed(tuple(self._directories.values())):
            try:
                directory.close()
            except OSError as error:
                if first_error is None:
                    first_error = error
        self._directories.clear()
        if first_error is not None:
            raise first_error

    def verify_namespace(self) -> None:
        """Fail if a pinned POSIX directory is no longer bound to its lexical name."""

        if os.name == "nt":
            # Windows pins deny delete/rename sharing, so the live handles are
            # themselves the namespace guard.
            return
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        verification: dict[Path, int] = {}
        try:
            verification[self.root] = os.open(self.root, flags)
            ordered = sorted(
                (path for path in self._directories if path != self.root),
                key=lambda path: (len(path.relative_to(self.root).parts), path.as_posix()),
            )
            for path in ordered:
                parent_descriptor = verification[path.parent]
                verification[path] = os.open(path.name, flags, dir_fd=parent_descriptor)
            for path, pinned in self._directories.items():
                if pinned.descriptor is None:  # pragma: no cover - defensive
                    raise RuntimeError("Pinned directory is closed")
                expected = os.fstat(pinned.descriptor)
                observed = os.fstat(verification[path])
                if (expected.st_dev, expected.st_ino) != (observed.st_dev, observed.st_ino):
                    raise JauntGenerationError(
                        f"Pinned workspace directory is no longer bound to its name: {path}"
                    )
        except JauntGenerationError:
            raise
        except OSError as error:
            raise JauntGenerationError(
                "Pinned workspace directory is no longer bound to its name"
            ) from error
        finally:
            for descriptor in reversed(tuple(verification.values())):
                with contextlib.suppress(OSError):
                    os.close(descriptor)

    def directory(self, path: Path, *, create: bool = True) -> _PinnedDirectory:
        absolute = Path(os.path.abspath(path if path.is_absolute() else self.root / path))
        try:
            relative = absolute.relative_to(self.root)
        except ValueError as error:
            raise JauntGenerationError(
                f"Refusing to pin a directory outside the workspace: {path}"
            ) from error
        if ".." in relative.parts:
            raise JauntGenerationError(f"Refusing to pin an unsafe workspace directory: {path}")

        current_path = self.root
        current = self._directories[self.root]
        for part in relative.parts:
            _PinnedDirectory._leaf(part)
            current_path /= part
            cached = self._directories.get(current_path)
            if cached is not None:
                current = cached
                continue
            created_now = False
            if os.name == "nt":
                if create:
                    try:
                        current_path.mkdir()
                    except FileExistsError:
                        pass
                    else:
                        created_now = True
                child = _PinnedDirectory(
                    path=current_path,
                    windows_handle=_windows_open_pinned_path(
                        current_path,
                        directory=True,
                        blocking=self.blocking,
                    ),
                    blocking=self.blocking,
                )
            else:
                if current.descriptor is None:  # pragma: no cover - defensive
                    raise RuntimeError("Pinned directory is closed")
                if create:
                    try:
                        os.mkdir(part, dir_fd=current.descriptor)
                    except FileExistsError:
                        pass
                    else:
                        created_now = True
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                try:
                    descriptor = os.open(part, flags, dir_fd=current.descriptor)
                except OSError as error:
                    if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                        raise JauntGenerationError(
                            f"Refusing to pin a redirecting workspace directory: {current_path}"
                        ) from error
                    raise
                child = _PinnedDirectory(
                    path=current_path,
                    descriptor=descriptor,
                    blocking=self.blocking,
                )
            try:
                current.fsync_required()
            except BaseException:
                child.close()
                raise
            self._directories[current_path] = child
            if created_now:
                self.created_directories.append(current_path)
            current = child
        return current


def _ensure_durable_directory(
    root: Path,
    directory: Path,
    *,
    synced_directories: set[Path] | None = None,
) -> tuple[Path, ...]:
    """Compatibility wrapper around a pinned, no-follow directory walk."""

    with _PinnedWorkspace(root) as workspace:
        workspace.directory(directory)
        created = tuple(workspace.created_directories)
        if synced_directories is not None:
            synced_directories.update(workspace._directories)
        return created


_TRANSACTION_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_TRANSACTION_SCHEME = "jaunt-ts-artifact-transaction/2"
_TRANSACTION_LOCK_NAME = ".atomic-write.lock"


@dataclass(slots=True)
class _TransactionLease:
    """One persistent-inode advisory lock held by a live transaction writer."""

    descriptor: int
    windows: bool
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        descriptor = self.descriptor
        self.released = True
        try:
            with contextlib.suppress(OSError):
                if self.windows:
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    locking = getattr(msvcrt, "locking", None)
                    unlock_mode = getattr(msvcrt, "LK_UNLCK", None)
                    if callable(locking) and isinstance(unlock_mode, int):
                        locking(descriptor, unlock_mode, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _transaction_lock_path(directory: Path) -> Path:
    return directory / _TRANSACTION_LOCK_NAME


def _acquire_transaction_lease(
    directory: Path,
    *,
    blocking: bool,
    pinned_directory: _PinnedDirectory | None = None,
    authority_directory: _PinnedDirectory | None = None,
) -> _TransactionLease | None:
    """Acquire the workspace's transaction lease, or ``None`` if busy.

    POSIX pinned callers lock a separately opened description of the workspace
    root inode. A transaction-directory rename therefore cannot split writers
    across two lock-file inodes. Windows keeps the lock file below its pinned
    no-write/no-delete directory chain.
    """

    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    windows = os.name == "nt"
    if not windows and authority_directory is not None:
        if authority_directory.descriptor is None:
            raise RuntimeError("Pinned authority directory is closed")
        authority_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(
            ".",
            authority_flags,
            dir_fd=authority_directory.descriptor,
        )
    elif pinned_directory is None:
        if not directory.is_dir():
            raise FileNotFoundError(errno.ENOENT, "Transaction directory does not exist", directory)
        descriptor = os.open(_transaction_lock_path(directory), flags, 0o600)
    else:
        descriptor = pinned_directory.open_lock(
            _TRANSACTION_LOCK_NAME,
            flags,
            0o600,
            blocking=blocking,
        )
    try:
        if windows:
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            locking = getattr(msvcrt, "locking", None)
            lock_mode = getattr(msvcrt, "LK_LOCK" if blocking else "LK_NBLCK", None)
            if not callable(locking) or not isinstance(lock_mode, int):
                raise RuntimeError("This Python runtime cannot lock TypeScript transactions")
            locking(descriptor, lock_mode, 1)
        else:
            import fcntl

            operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            fcntl.flock(descriptor, operation)
    except OSError as error:
        os.close(descriptor)
        busy_errors = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}
        if not blocking and error.errno in busy_errors:
            return None
        raise
    except BaseException:
        os.close(descriptor)
        raise
    return _TransactionLease(descriptor=descriptor, windows=windows)


def _write_transaction_manifest(
    manifest: Path,
    payload: Mapping[str, Any],
    *,
    pinned_directory: _PinnedDirectory | None = None,
) -> None:
    """Atomically replace one transaction marker and durably publish its state."""

    if pinned_directory is None and not manifest.parent.is_dir():
        raise FileNotFoundError(
            errno.ENOENT,
            "Transaction directory does not exist",
            manifest.parent,
        )
    if pinned_directory is None:
        fd, raw_temporary = tempfile.mkstemp(
            prefix=f".{manifest.name}.", suffix=".tmp", dir=manifest.parent
        )
        temporary_name = Path(raw_temporary).name
    else:
        fd, temporary_name = pinned_directory.create_temp(
            prefix=f".{manifest.name}.", suffix=".tmp"
        )
    try:
        try:
            with os.fdopen(
                fd,
                "w",
                encoding="utf-8",
                newline="\n",
                closefd=False,
            ) as handle:
                json.dump(payload, handle, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(fd)
        if pinned_directory is None:
            os.replace(manifest.parent / temporary_name, manifest)
            _fsync_directory_required(manifest.parent)
        else:
            pinned_directory.replace(temporary_name, manifest.name)
            pinned_directory.fsync_required()
    finally:
        if pinned_directory is None:
            with contextlib.suppress(FileNotFoundError):
                (manifest.parent / temporary_name).unlink()
        else:
            pinned_directory.unlink(temporary_name, missing_ok=True)


def _retire_transaction_manifest(
    manifest: Path,
    payload: Mapping[str, Any],
    *,
    pinned_directory: _PinnedDirectory | None = None,
) -> bool:
    """Durably remove a marker, restoring it when the directory sync fails."""

    try:
        if pinned_directory is None:
            manifest.unlink(missing_ok=True)
        else:
            pinned_directory.unlink(manifest.name, missing_ok=True)
    except OSError:
        return False
    try:
        if pinned_directory is None:
            _fsync_directory_required(manifest.parent)
        else:
            pinned_directory.fsync_required()
    except OSError:
        # The unlink is not a durable fact. Re-publish the exact conservative
        # marker so this process also continues to report the transaction.
        with contextlib.suppress(OSError):
            _write_transaction_manifest(
                manifest,
                payload,
                pinned_directory=pinned_directory,
            )
        return False
    return True


def _recorded_transaction_hash(value: object) -> str | None:
    if value == MISSING_INPUT:
        return None
    if isinstance(value, str) and _TRANSACTION_HASH.fullmatch(value) is not None:
        return value
    raise ValueError("invalid transaction hash")


def _recover_atomic_write_manifests(root: Path) -> tuple[Path, ...]:
    """Retire conclusively committed or unapplied ``atomic_write_manifest`` markers.

    ``prepared`` plus unanimous before-hashes proves that no replacement is
    visible. ``committed`` plus unanimous after-hashes proves both byte
    convergence and that the transaction's final runtime seal completed. Legacy
    markers without the persistent-lease scheme are deliberately left blocking:
    their hashes cannot prove that no older writer is still live, and proposed
    output bytes do not prove that the final seal ran.
    """

    root = root.resolve()
    directory = root / ".jaunt" / "transactions"
    try:
        workspace = _PinnedWorkspace(root, blocking=False)
    except OSError:
        return ()
    with workspace:
        try:
            pinned_directory = workspace.directory(directory, create=False)
        except FileNotFoundError:
            return ()
        try:
            lease = _acquire_transaction_lease(
                directory,
                blocking=False,
                pinned_directory=pinned_directory,
                authority_directory=workspace.root_directory,
            )
        except OSError:
            return ()
        if lease is None:
            return ()
        recovered: list[Path] = []
        try:
            for manifest_name in pinned_directory.iter_names("ts-*.json"):
                manifest = directory / manifest_name
                try:
                    raw_payload = json.loads(
                        pinned_directory.read_bytes(manifest_name).decode("utf-8")
                    )
                    if not isinstance(raw_payload, Mapping):
                        continue
                    payload = {str(key): value for key, value in raw_payload.items()}
                    if payload.get("scheme") != _TRANSACTION_SCHEME:
                        continue
                    state = payload.get("state")
                    if state not in {"prepared", "committed"}:
                        continue
                    raw_writes = payload.get("writes")
                    if not isinstance(raw_writes, list) or not raw_writes:
                        continue
                    entries: list[tuple[Path, str | None, str | None]] = []
                    seen: set[Path] = set()
                    for raw_write in raw_writes:
                        if not isinstance(raw_write, Mapping):
                            raise ValueError("invalid transaction write")
                        relative = raw_write.get("path")
                        kind = raw_write.get("kind")
                        module_id = raw_write.get("moduleId")
                        if (
                            not isinstance(relative, str)
                            or not relative
                            or relative == "."
                            or not isinstance(kind, str)
                            or not kind
                            or not isinstance(module_id, str)
                            or not module_id
                        ):
                            raise ValueError("invalid transaction write")
                        _safe_path(root, relative)
                        path = root / Path(relative)
                        if path in seen:
                            raise ValueError("duplicate transaction path")
                        seen.add(path)
                        entries.append(
                            (
                                path,
                                _recorded_transaction_hash(raw_write.get("before")),
                                _recorded_transaction_hash(raw_write.get("after")),
                            )
                        )
                    expected = (
                        tuple((path, after) for path, _before, after in entries)
                        if state == "committed"
                        else tuple((path, before) for path, before, _after in entries)
                    )
                    current: list[tuple[Path, str | None]] = []
                    for path, _digest in expected:
                        try:
                            output_directory = workspace.directory(path.parent, create=False)
                        except FileNotFoundError:
                            digest = None
                        else:
                            digest = output_directory.path_hash(path.name)
                        current.append((path, digest))
                    if any(
                        digest != expected_digest
                        for (_path, digest), (_expected_path, expected_digest) in zip(
                            current, expected, strict=True
                        )
                    ):
                        continue
                    workspace.verify_namespace()
                except (
                    JauntConfigError,
                    JauntGenerationError,
                    OSError,
                    UnicodeError,
                    ValueError,
                    json.JSONDecodeError,
                ):
                    continue
                if _retire_transaction_manifest(
                    manifest,
                    payload,
                    pinned_directory=pinned_directory,
                ):
                    recovered.append(manifest)
        finally:
            lease.release()
        return tuple(recovered)


def _has_pending_atomic_write_manifests(root: Path) -> bool:
    """Fail closed when an artifact transaction is active or unresolved."""

    root = root.resolve()
    directory = root / ".jaunt" / "transactions"
    try:
        workspace = _PinnedWorkspace(root, blocking=False)
    except OSError:
        return True
    with workspace:
        try:
            pinned_directory = workspace.directory(directory, create=False)
        except FileNotFoundError:
            return False
        try:
            lease = _acquire_transaction_lease(
                directory,
                blocking=False,
                pinned_directory=pinned_directory,
                authority_directory=workspace.root_directory,
            )
        except OSError:
            return True
        if lease is None:
            return True
        try:
            return bool(pinned_directory.iter_names("*.json"))
        except OSError:
            return True
        finally:
            lease.release()


def _artifact_preconditions(root: Path, modules: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for module in modules:
        for key in ("facadePath", "apiMirrorPath", "implementationPath", "sidecarPath"):
            relative = _module_path(module, key)
            result[relative] = _path_hash(_safe_path(root, relative)) or MISSING_INPUT
    return result


def _input_hashes(value: Mapping[str, Any]) -> Mapping[str, str]:
    hashes = value.get("inputHashes", {})
    if not isinstance(hashes, Mapping):
        return {}
    return {str(path): str(digest) for path, digest in hashes.items() if isinstance(digest, str)}


def _assert_inputs_unchanged(root: Path, expected: Mapping[str, str]) -> None:
    changed: list[str] = []
    for relative, digest in sorted(expected.items()):
        path = _safe_path(root, relative)
        actual = _path_hash(path)
        if digest == MISSING_INPUT:
            if actual is not None:
                changed.append(relative)
            continue
        normalized = digest if digest.startswith("sha256:") else f"sha256:{digest}"
        if actual != normalized:
            changed.append(relative)
    if changed:
        raise JauntGenerationError(
            "TypeScript inputs changed after analysis; no artifacts were written: "
            + ", ".join(changed)
        )


def _diagnostic(value: ProtocolDiagnostic | Mapping[str, Any]) -> TargetDiagnostic:
    if isinstance(value, ProtocolDiagnostic):
        return TargetDiagnostic(
            code=value.code,
            message=value.message,
            severity=cast(
                Any, value.severity if value.severity in {"error", "warning", "info"} else "error"
            ),
            path=value.path,
            line=value.line,
            column=value.column,
            data={"start": value.start, "end": value.end},
        )
    severity = value.get("severity", "error")
    return TargetDiagnostic(
        code=str(value.get("code", "JAUNT_TS_DIAGNOSTIC")),
        message=str(value.get("message", "TypeScript operation failed")),
        severity=cast(Any, severity if severity in {"error", "warning", "info"} else "error"),
        path=str(value["path"]) if value.get("path") is not None else None,
        line=value.get("line") if isinstance(value.get("line"), int) else None,
        column=value.get("column") if isinstance(value.get("column"), int) else None,
        data={
            str(key): item
            for key, item in value.items()
            if key not in {"code", "message", "severity", "path", "line", "column"}
        },
    )


def _diagnostics(
    values: Sequence[ProtocolDiagnostic | Mapping[str, Any]],
) -> tuple[TargetDiagnostic, ...]:
    unique: dict[tuple[object, ...], TargetDiagnostic] = {}
    for value in values:
        diagnostic = _diagnostic(value)
        key = (
            diagnostic.code,
            diagnostic.message,
            diagnostic.severity,
            diagnostic.path,
            diagnostic.line,
            diagnostic.column,
        )
        unique.setdefault(key, diagnostic)
    return tuple(unique.values())


def _module_closure_ids(
    modules: Sequence[Mapping[str, Any]], requested: Sequence[str]
) -> tuple[str, ...]:
    by_id = {_module_id(module): module for module in modules}
    selected: set[str] = set()
    pending = [module_id for module_id in requested if module_id in by_id]
    while pending:
        module_id = pending.pop()
        if module_id in selected:
            continue
        selected.add(module_id)
        pending.extend(
            dependency
            for dependency in _dependency_module_ids(by_id[module_id])
            if dependency in by_id
        )
    return tuple(_module_id(module) for module in modules if _module_id(module) in selected)


def _initialize_params(
    root: Path,
    config: JauntConfig,
    target: TypeScriptTargetConfig,
    client: WorkerLike,
    *,
    generation_fingerprint: str | None = None,
) -> InitializeParams:
    installation = getattr(client, "installation", None)
    compiler = getattr(installation, "compiler_module_path", Path("typescript/lib/typescript.js"))
    return InitializeParams(
        root=str(root.resolve()),
        projects=tuple(target.projects),
        test_projects=tuple(target.test_projects),
        source_roots=tuple(target.source_roots),
        test_roots=tuple(target.test_roots),
        generated_dir=target.generated_dir,
        tool_owner=target.tool_owner,
        compiler_module_path=str(compiler),
        client_version=jaunt.__version__,
        tool_version=jaunt.__version__,
        generation_fingerprint=generation_fingerprint or _generation_fingerprint(config, root=root),
    )


@asynccontextmanager
async def worker_session(
    root: Path,
    config: JauntConfig,
    *,
    worker_factory: WorkerFactory | None = None,
    generation_fingerprint: str | None = None,
) -> AsyncIterator[tuple[WorkerLike, InitializeResult]]:
    """Open and initialize one project-local analyzer session."""

    root = root.resolve()
    # A killed test-repair process can leave an implementation commit between
    # its model pass and held-out rerun. Restore the durable pre-repair snapshot
    # before any subsequent TypeScript operation observes that state.
    from jaunt.typescript.tester import _recover_pending_test_repairs

    _recover_pending_test_repairs(root)
    target = _target(config)
    if worker_factory is None:
        installation = resolve_worker_installation(root, target)
        client: WorkerLike = WorkerClient(
            root=root,
            installation=installation,
            request_timeout=target.worker_timeout_seconds,
            startup_timeout=target.worker_startup_timeout_seconds,
            heap_mb=target.worker_heap_mb,
        )
    else:
        created = worker_factory(root, target)
        client = await created if inspect.isawaitable(created) else created

    entered = False
    enter = getattr(client, "__aenter__", None)
    exit_ = getattr(client, "__aexit__", None)
    active_exception: BaseException | None = None
    try:
        if callable(enter):
            entered_client = await enter()
            if entered_client is not None:
                client = cast(WorkerLike, entered_client)
            entered = True
        initialized = await client.initialize(
            _initialize_params(
                root,
                config,
                target,
                client,
                generation_fingerprint=generation_fingerprint,
            )
        )
        validate_worker_capabilities(initialized)
        yield client, initialized
    except BaseException as exc:
        active_exception = exc
        raise
    finally:
        if entered and callable(exit_):
            if active_exception is None:
                await exit_(None, None, None)
            else:
                await exit_(
                    type(active_exception),
                    active_exception,
                    active_exception.__traceback__,
                )
        elif not entered:
            close = getattr(client, "close", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result


async def analyze(
    client: WorkerLike,
    initialized: InitializeResult,
    *,
    target_ids: Sequence[str] = (),
) -> TypeScriptAnalysis:
    supports_scoped_diagnostics = "scoped-diagnostics" in getattr(initialized, "capabilities", ())
    workspace = await client.request(
        "analyzeWorkspace",
        ({"moduleIds": list(target_ids)} if target_ids and supports_scoped_diagnostics else {}),
    )
    supports_scoped_analysis = "scoped-analysis" in getattr(initialized, "capabilities", ())
    raw_specs = workspace.get("specs", [])
    workspace_module_ids = (
        list(
            dict.fromkeys(
                str(item["moduleId"])
                for item in raw_specs
                if isinstance(item, Mapping) and isinstance(item.get("moduleId"), str)
            )
        )
        if isinstance(raw_specs, list)
        else []
    )
    requested_module_ids = list(dict.fromkeys(target.split("#", 1)[0] for target in target_ids))
    if target_ids and supports_scoped_analysis:
        batches = [requested_module_ids]
    elif not target_ids and workspace_module_ids:
        batches = [
            workspace_module_ids[index : index + _ANALYZE_CONTRACT_BATCH_SIZE]
            for index in range(0, len(workspace_module_ids), _ANALYZE_CONTRACT_BATCH_SIZE)
        ]
    else:
        batches = [[]]
    contract_responses = [
        await client.request(
            "analyzeContracts",
            ({"moduleIds": batch} if batch else {}),
        )
        for batch in batches
    ]
    contracts = dict(contract_responses[0])
    merged_modules: dict[str, Mapping[str, Any]] = {}
    for response in contract_responses:
        raw_modules = response.get("modules", [])
        if not isinstance(raw_modules, list):
            continue
        for module in raw_modules:
            if isinstance(module, Mapping):
                merged_modules[_module_id(module)] = module
    contracts["modules"] = list(merged_modules.values())
    if target_ids:
        raw_modules = contracts.get("modules", [])
        modules = (
            [item for item in raw_modules if isinstance(item, Mapping)]
            if isinstance(raw_modules, list)
            else []
        )
        by_id = {_module_id(module): module for module in modules}
        requested = {target.split("#", 1)[0] for target in target_ids}
        missing = set(requested - set(by_id))
        for target in target_ids:
            module_id, separator, symbol_name = target.partition("#")
            if not separator or module_id not in by_id:
                continue
            symbols = by_id[module_id].get("symbols", [])
            names = (
                {
                    str(symbol.get("name"))
                    for symbol in symbols
                    if isinstance(symbol, Mapping) and isinstance(symbol.get("name"), str)
                }
                if isinstance(symbols, list)
                else set()
            )
            if symbol_name not in names:
                missing.add(target)
        if missing:
            raise JauntConfigError("Unknown TypeScript target(s): " + ", ".join(sorted(missing)))
        if not supports_scoped_analysis:
            selected: set[str] = set()
            pending = list(requested)
            while pending:
                module_id = pending.pop()
                if module_id in selected:
                    continue
                selected.add(module_id)
                dependencies = by_id[module_id].get("dependencies", [])
                if isinstance(dependencies, list):
                    pending.extend(
                        dependency.split("#", 1)[0]
                        for dependency in dependencies
                        if isinstance(dependency, str) and dependency.split("#", 1)[0] in by_id
                    )
            contracts = {
                **contracts,
                "modules": [module for module in modules if _module_id(module) in selected],
            }
    diagnostics = workspace.get("diagnostics", [])
    errors = (
        [
            item
            for item in diagnostics
            if isinstance(item, Mapping) and item.get("severity") == "error"
        ]
        if isinstance(diagnostics, list)
        else []
    )
    if errors:
        rendered = "; ".join(
            str(item.get("message", "TypeScript discovery failed")) for item in errors
        )
        raise JauntConfigError(f"TypeScript workspace analysis failed: {rendered}")
    return TypeScriptAnalysis(initialized=initialized, workspace=workspace, contracts=contracts)


def _stamp_params(
    analysis: TypeScriptAnalysis,
    candidates: Mapping[str, str],
    module_ids: Sequence[str],
) -> ValidateOverlayParams:
    source = analysis.contracts
    return ValidateOverlayParams(
        session_id=str(source.get("sessionId", analysis.initialized.stamp.session_id)),
        expected_epoch=int(source.get("epoch", analysis.initialized.stamp.epoch)),
        expected_snapshot=str(source.get("snapshot", analysis.initialized.stamp.snapshot)),
        candidates=candidates,
        module_ids=tuple(module_ids),
    )


async def validate_overlay(
    client: WorkerLike,
    analysis: TypeScriptAnalysis,
    candidates: Mapping[str, str],
    module_ids: Sequence[str],
    *,
    sync_module_ids: Sequence[str] = (),
    restamp_module_ids: Sequence[str] = (),
    recompose_module_ids: Sequence[str] = (),
    scoped_validation: bool = False,
    baseline_unselected: bool = False,
    release_programs: bool = False,
) -> ValidateOverlayResult:
    wire = _stamp_params(analysis, candidates, module_ids).to_wire()
    if sync_module_ids:
        wire["syncModuleIds"] = list(sync_module_ids)
    if restamp_module_ids:
        wire["restampModuleIds"] = list(restamp_module_ids)
    if recompose_module_ids:
        wire["recomposeModuleIds"] = list(recompose_module_ids)
    if scoped_validation and "scoped-validation" in analysis.initialized.capabilities:
        wire["scopeToModuleIds"] = True
    if baseline_unselected:
        wire["baselineUnselected"] = True
    if release_programs:
        wire["releasePrograms"] = True
    raw = await client.request(
        "validateOverlay",
        wire,
    )
    return ValidateOverlayResult.from_wire(raw)


def _artifact_writes(result: ValidateOverlayResult) -> tuple[_Write, ...]:
    writes: list[_Write] = []
    for artifact in result.artifacts:
        if _sha256(artifact.content.encode("utf-8")) != artifact.sha256:
            raise JauntGenerationError(
                f"TypeScript worker returned a bad content hash for {artifact.path}"
            )
        writes.append(
            _Write(
                path=artifact.path,
                content=artifact.content,
                kind=artifact.kind,
                module_id=artifact.module_id,
            )
        )
    return tuple(writes)


def atomic_write_manifest(
    root: Path,
    writes: Sequence[_Write],
    *,
    expected_inputs: Mapping[str, str] | None = None,
    preserve_existing_facades: bool = False,
    preserve_real_implementations: bool = False,
    pre_commit_guard: Callable[[], None] | None = None,
    commit_seal: Callable[[], None] | None = None,
    allowed_transaction_manifests: Sequence[str] = (),
) -> tuple[_Write, ...]:
    """Replace a validated artifact manifest with preconditions and rollback.

    ``os.replace`` is atomic per path.  The explicit rollback makes the multi-file
    transaction recoverable if a later replacement fails. ``pre_commit_guard``
    validates external state before replacement; ``commit_seal`` finalizes that
    state after byte convergence while originals are still available for rollback.
    """

    root = root.resolve()
    expected_inputs = expected_inputs or {}
    _assert_inputs_unchanged(root, expected_inputs)

    with _PinnedWorkspace(root) as workspace:
        filtered: list[_Write] = []
        paths: dict[int, Path] = {}
        seen: set[Path] = set()
        for write in writes:
            # Keep the worker-supplied lexical path. The pinned walk, rather
            # than a resolved string path, is the publication authority.
            _safe_path(root, write.path)
            path = root / Path(write.path)
            try:
                existing_directory = workspace.directory(path.parent, create=False)
            except FileNotFoundError:
                existing_metadata = None
                existing_directory = None
            else:
                existing_metadata = existing_directory.stat(path.name)
            if write.kind == "facade" and preserve_existing_facades:
                if existing_metadata is not None:
                    continue
            if write.kind in {"implementation", "placeholder"} and preserve_real_implementations:
                if existing_metadata is not None and existing_directory is not None:
                    existing = existing_directory.read_bytes(path.name).decode("utf-8")
                    if not any(marker in existing for marker in _PLACEHOLDER_MARKERS):
                        continue
            if path in seen:
                raise JauntGenerationError(f"Duplicate TypeScript artifact path: {write.path}")
            seen.add(path)
            paths[id(write)] = path
            filtered.append(write)
        if not filtered:
            return ()

        original: dict[Path, bytes | None] = {}
        observed: dict[Path, str | None] = {}
        output_directories: dict[Path, _PinnedDirectory] = {}
        staged: dict[Path, tuple[_PinnedDirectory, str]] = {}
        manifest: Path | None = None
        manifest_payload: dict[str, Any] | None = None
        transaction_lease: _TransactionLease | None = None
        try:
            for write in filtered:
                path = paths[id(write)]
                directory = workspace.directory(path.parent)
                output_directories[path] = directory
                try:
                    old = directory.read_bytes(path.name)
                except FileNotFoundError:
                    old = None
                original[path] = old
                observed[path] = _sha256(old) if old is not None else None
                if write.content is None:
                    continue
                descriptor, temporary_name = directory.create_temp(f".{path.name}.jaunt-")
                staged[path] = (directory, temporary_name)
                try:
                    with os.fdopen(descriptor, "wb", closefd=False) as handle:
                        handle.write(write.content.encode("utf-8"))
                        handle.flush()
                        os.fsync(handle.fileno())
                finally:
                    os.close(descriptor)

            writes_by_path = {paths[id(write)]: write for write in filtered}
            manifest_directory = root / ".jaunt" / "transactions"
            pinned_manifest_directory = workspace.directory(manifest_directory)
            manifest = manifest_directory / f"ts-{uuid.uuid4().hex}.json"
            transaction_lease = _acquire_transaction_lease(
                manifest_directory,
                blocking=True,
                pinned_directory=pinned_manifest_directory,
                authority_directory=workspace.root_directory,
            )
            if transaction_lease is None:  # pragma: no cover - blocking acquisition
                raise JauntGenerationError("Could not acquire TypeScript transaction lease")

            # Recheck every publication precondition under the global lease.
            allowed_manifests = set(allowed_transaction_manifests)
            if any(
                Path(name).name != name or _unsafe_portable_leaf(name) for name in allowed_manifests
            ):
                raise JauntGenerationError("Invalid TypeScript transaction manifest allowlist")
            present_manifests = pinned_manifest_directory.iter_names("*.json")
            missing_allowed_manifests = tuple(sorted(allowed_manifests - set(present_manifests)))
            if missing_allowed_manifests:
                raise JauntGenerationError(
                    "An allowed TypeScript transaction manifest is missing: "
                    + ", ".join(missing_allowed_manifests)
                )
            pending_manifests = tuple(
                name for name in present_manifests if name not in allowed_manifests
            )
            if pending_manifests:
                raise JauntGenerationError(
                    "An unresolved TypeScript artifact transaction blocks publication: "
                    + ", ".join(pending_manifests)
                )
            _assert_inputs_unchanged(root, expected_inputs)
            for path, expected in observed.items():
                if output_directories[path].path_hash(path.name) != expected:
                    raise JauntGenerationError(
                        f"TypeScript artifact changed during validation: {path.relative_to(root)}"
                    )
            if pre_commit_guard is not None:
                pre_commit_guard()
            workspace.verify_namespace()

            manifest_payload = {
                "scheme": _TRANSACTION_SCHEME,
                "state": "prepared",
                "writes": [
                    {
                        "path": path.relative_to(root).as_posix(),
                        "kind": writes_by_path[path].kind,
                        "moduleId": writes_by_path[path].module_id,
                        "before": observed[path] or MISSING_INPUT,
                        "after": (
                            _sha256(cast(str, writes_by_path[path].content).encode("utf-8"))
                            if writes_by_path[path].content is not None
                            else MISSING_INPUT
                        ),
                    }
                    for path in sorted(writes_by_path, key=lambda item: item.as_posix())
                ],
            }
            _write_transaction_manifest(
                manifest,
                manifest_payload,
                pinned_directory=pinned_manifest_directory,
            )

            replaced: list[Path] = []
            committed_payload: dict[str, Any] | None = None
            try:
                for path in sorted(writes_by_path, key=lambda item: item.as_posix()):
                    write = writes_by_path[path]
                    directory = output_directories[path]
                    if write.content is None:
                        directory.unlink(path.name, missing_ok=True)
                    else:
                        directory.replace(staged[path][1], path.name)
                    # The path is visible now, so include it in rollback before
                    # the required durability check.
                    replaced.append(path)
                    directory.fsync_required()
                unconverged = [
                    path
                    for path, write in writes_by_path.items()
                    if output_directories[path].path_hash(path.name)
                    != (
                        _sha256(write.content.encode("utf-8"))
                        if write.content is not None
                        else None
                    )
                ]
                if unconverged:
                    module_ids = sorted({writes_by_path[path].module_id for path in unconverged})
                    raise JauntGenerationError(
                        "TypeScript artifact transaction did not converge after commit for "
                        + ", ".join(module_ids)
                        + ": "
                        + ", ".join(
                            path.relative_to(root).as_posix() for path in sorted(unconverged)
                        )
                    )
                workspace.verify_namespace()
                if commit_seal is not None:
                    commit_seal()
                workspace.verify_namespace()
                committed_payload = {**manifest_payload, "state": "committed"}
                _write_transaction_manifest(
                    manifest,
                    committed_payload,
                    pinned_directory=pinned_manifest_directory,
                )
                workspace.verify_namespace()
            except BaseException:
                rollback_ok = True
                for path in reversed(replaced):
                    old = original[path]
                    directory = output_directories[path]
                    try:
                        if old is None:
                            directory.unlink(path.name, missing_ok=True)
                            directory.fsync_required()
                        else:
                            descriptor, temporary_name = directory.create_temp(
                                f".{path.name}.rollback-"
                            )
                            try:
                                try:
                                    with os.fdopen(descriptor, "wb", closefd=False) as handle:
                                        handle.write(old)
                                        handle.flush()
                                        os.fsync(handle.fileno())
                                finally:
                                    os.close(descriptor)
                                directory.replace(temporary_name, path.name)
                                directory.fsync_required()
                            finally:
                                directory.unlink(temporary_name, missing_ok=True)
                    except OSError:
                        rollback_ok = False
                if rollback_ok and manifest is not None and manifest_payload is not None:
                    _retire_transaction_manifest(
                        manifest,
                        manifest_payload,
                        pinned_directory=pinned_manifest_directory,
                    )
                raise
            else:
                if (
                    manifest is None or manifest_payload is None or committed_payload is None
                ):  # pragma: no cover - defensive
                    raise JauntGenerationError("TypeScript artifact transaction lost its manifest")
                if not _retire_transaction_manifest(
                    manifest,
                    committed_payload,
                    pinned_directory=pinned_manifest_directory,
                ):
                    raise JauntGenerationError(
                        "TypeScript artifact transaction committed, but its recovery marker "
                        "could not be durably retired"
                    )
        finally:
            active_error = sys.exception()
            cleanup_error: OSError | None = None
            try:
                for directory, temporary_name in staged.values():
                    try:
                        directory.unlink(temporary_name, missing_ok=True)
                    except OSError as error:
                        if cleanup_error is None:
                            cleanup_error = error
            finally:
                if transaction_lease is not None:
                    try:
                        transaction_lease.release()
                    except OSError as error:
                        if cleanup_error is None:
                            cleanup_error = error
            if cleanup_error is not None:
                if active_error is None:
                    raise cleanup_error
                active_error.add_note(
                    f"TypeScript transaction cleanup also failed: {cleanup_error}"
                )
        return tuple(filtered)


def _module_id(module: Mapping[str, Any]) -> str:
    value = module.get("moduleId")
    if not isinstance(value, str) or not value.startswith("ts:"):
        raise JauntConfigError("TypeScript worker returned a module without a stable ts: ID")
    return value


def _model_contract(module: Mapping[str, Any]) -> dict[str, Any]:
    """Return analyzer contract data without tool-only provenance records."""

    contract = {
        str(key): value
        for key, value in module.items()
        if key not in {"toolingProvenanceRecords", "typeContext"}
    }
    authored_context, _imported_context = _split_context_source(contract.get("contextSource"))
    if authored_context is None:
        contract.pop("contextSource", None)
    else:
        contract["contextSource"] = authored_context
    sidecar = contract.get("sidecar")
    if isinstance(sidecar, str):
        try:
            sidecar_payload = json.loads(sidecar)
        except json.JSONDecodeError:
            contract.pop("sidecar", None)
        else:
            if isinstance(sidecar_payload, dict):
                sidecar_payload.pop("toolingProvenanceRecords", None)
                contract["sidecar"] = (
                    json.dumps(sidecar_payload, sort_keys=True, indent=2, default=str) + "\n"
                )
            else:
                contract.pop("sidecar", None)
    return contract


def _split_context_source(value: object) -> tuple[str | None, str | None]:
    """Separate authored context from the worker's appended type transport."""

    if not isinstance(value, str):
        return None, None
    candidates = [
        (value.rfind(marker), marker)
        for marker in (
            _LEGACY_IMPORTED_TYPE_CONTEXT_BEGIN,
            _IMPORTED_TYPE_CONTEXT_BEGIN,
        )
        if marker in value
    ]
    if not candidates:
        return value, None
    begin, marker = max(candidates, key=lambda item: item[0])
    end = value.find(
        _IMPORTED_TYPE_CONTEXT_END,
        begin + len(marker),
    )
    if end < 0:
        return value, None
    authored = value[:begin].rstrip()
    imported = value[begin + len(marker) : end]
    return (f"{authored}\n" if authored else None), imported


def _module_path(module: Mapping[str, Any], key: str) -> str:
    value = module.get(key)
    if not isinstance(value, str) or not value:
        routes = module.get("routes")
        if isinstance(routes, Mapping):
            value = routes.get(key)
    if not isinstance(value, str) or not value:
        raise JauntConfigError(f"TypeScript worker omitted {key} for {_module_id(module)}")
    return value


async def run_sync(
    root: Path,
    config: JauntConfig,
    *,
    target_ids: Sequence[str] = (),
    worker_factory: WorkerFactory | None = None,
) -> SyncReport:
    """Render mirrors/facades/placeholders without invoking a model."""

    root = root.resolve()
    async with worker_session(root, config, worker_factory=worker_factory) as (client, initialized):
        analysis = await analyze(client, initialized, target_ids=target_ids)
        modules = _topological_modules(analysis.modules)
        output_preconditions = _artifact_preconditions(root, modules)
        module_ids = tuple(_module_id(module) for module in modules)
        validated_batches: list[ValidateOverlayResult] = []
        failed: dict[str, tuple[TargetDiagnostic, ...]] = {}
        for index in range(0, len(module_ids), _SYNC_BATCH_SIZE):
            batch = module_ids[index : index + _SYNC_BATCH_SIZE]
            closure = _module_closure_ids(modules, batch)
            validated = await validate_overlay(
                client,
                analysis,
                {},
                closure,
                sync_module_ids=batch,
                scoped_validation=True,
                baseline_unselected=True,
            )
            if not validated.valid:
                diagnostics = _diagnostics(validated.diagnostics)
                paths = {
                    _module_path(module, key): _module_id(module)
                    for module in modules
                    for key in ("specPath", "facadePath", "apiMirrorPath", "implementationPath")
                    if (
                        isinstance(module.get(key), str)
                        or isinstance(module.get("routes"), Mapping)
                    )
                }
                owners = {
                    paths[diagnostic.path] for diagnostic in diagnostics if diagnostic.path in paths
                }
                for module_id in sorted(owners or set(batch)):
                    failed[module_id] = diagnostics
                continue
            validated_batches.append(validated)
        validated_writes = tuple(
            write for validated in validated_batches for write in _artifact_writes(validated)
        )
        writes = atomic_write_manifest(
            root,
            validated_writes,
            expected_inputs={
                **_input_hashes(analysis.contracts),
                **output_preconditions,
            },
            preserve_existing_facades=True,
            preserve_real_implementations=True,
            pre_commit_guard=lambda: _verify_worker_runtime_identity(client),
            commit_seal=lambda: _seal_worker_runtime_identity(client),
        )

    append_events(
        root,
        [JournalEvent("sync", write.module_id, write.path) for write in writes],
    )
    return SyncReport(
        mirrors=tuple(sorted(write.path for write in writes if write.kind == "api-mirror")),
        placeholders=tuple(sorted(write.path for write in writes if write.kind == "placeholder")),
        created_facades=tuple(sorted(write.path for write in writes if write.kind == "facade")),
        failed=failed,
        exit_code=2 if failed else 0,
    )


def _prompt_text(configured: str, bundled: str) -> str:
    path = Path(configured) if configured else Path(__file__).with_name("prompts") / bundled
    return path.read_text(encoding="utf-8").strip()


def _generation_fingerprint(
    config: JauntConfig,
    *,
    root: Path | None = None,
    build_instructions: Sequence[str] | None = None,
    builtin_skill_names: Sequence[str] | None = None,
    repo_map_enabled: bool | None = None,
    project_overview_enabled: bool | None = None,
) -> str:
    """Hash every input that can change model-authored implementation bytes."""

    instructions = (
        tuple(build_instructions)
        if build_instructions is not None
        else tuple(config.build.instructions)
    )
    effective_builtin_skills = (
        tuple(builtin_skill_names)
        if builtin_skill_names is not None
        else (tuple(config.skills.builtin_skills) if config.skills.builtin else ())
    )
    payload = {
        "format": "jaunt-ts-generation/1-draft.1",
        "model": config.codex.model,
        "reasoning_effort": config.codex.reasoning_effort,
        "sandbox": config.codex.sandbox,
        "codex_features": tuple(config.codex.features),
        "codex_config": config.codex.config,
        "build_system": _prompt_text(config.typescript_prompts.build_system, "build_system.md"),
        "build_module": _prompt_text(config.typescript_prompts.build_module, "build_module.md"),
        "build_instructions": instructions,
        "repo_map_enabled": (
            bool(config.context.repo_map) if repo_map_enabled is None else repo_map_enabled
        ),
        "project_overview_enabled": (
            bool(config.context.overview)
            if project_overview_enabled is None
            else project_overview_enabled
        ),
        "builtin_skills": effective_builtin_skills,
        "skills_fingerprint": skills_fingerprint(
            project_root=root,
            builtin_names=effective_builtin_skills,
        ),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def _typescript_project_digest(
    modules: Sequence[Mapping[str, Any]],
    repo_map_block: str,
) -> str:
    """Digest the exact TS contracts used to generate a project overview."""

    payload = [
        {
            "moduleId": _module_id(module),
            "specSource": str(module.get("specSource", "")),
            "apiSource": str(module.get("apiSource", "")),
        }
        for module in sorted(modules, key=_module_id)
    ]
    return hashlib.sha256(
        json.dumps(
            {"modules": payload, "repoMap": repo_map_block},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


async def _project_overview_block(
    root: Path,
    config: JauntConfig,
    modules: Sequence[Mapping[str, Any]],
    *,
    repo_map_block: str,
    backend: GeneratorBackend,
    cost_tracker: CostTracker,
    enabled: bool,
) -> str:
    """Build/reuse the common project overview for a TypeScript workspace."""

    if not enabled:
        return ""
    from jaunt.repo_context.overview import build_project_docs_block, load_or_build_overview

    docs = build_project_docs_block(root, max_chars=config.context.max_chars)
    try:
        result = await load_or_build_overview(
            backend,
            repo_map_block=repo_map_block,
            project_docs=docs,
            digest=_typescript_project_digest(modules, repo_map_block),
            # Keep the TS overview cache separate from Python's project overview.
            # Mixed commands may build both concurrently and each digest covers a
            # different contract set.
            state_dir=root / ".jaunt" / "typescript",
            enabled=True,
            prompts=config.prompts,
            cost_tracker=cost_tracker,
        )
        cost_tracker.check_budget()
        return result
    except JauntGenerationError:
        raise
    except Exception:
        # Match Python's best-effort context behavior: an unavailable overview must
        # never block otherwise-valid generation.
        return ""


def _progress_phase(progress: object | None, item: str, stage: str, detail: str = "") -> None:
    if progress is None:
        return
    phase = getattr(progress, "phase", None)
    if callable(phase):
        try:
            phase(item, stage, detail)
        except Exception:
            pass


def _progress_advance(progress: object | None, item: str, *, ok: bool) -> None:
    if progress is None:
        return
    advance = getattr(progress, "advance", None)
    if callable(advance):
        try:
            advance(item, ok=ok)
        except Exception:
            pass


def _progress_finish(progress: object | None) -> None:
    if progress is None:
        return
    finish = getattr(progress, "finish", None)
    if callable(finish):
        try:
            finish()
        except Exception:
            pass


def _progress_set_total(progress: object | None, total: int) -> None:
    if progress is None:
        return
    set_total = getattr(progress, "set_total", None)
    if callable(set_total):
        try:
            set_total(total)
        except Exception:
            pass


def _progress_reset(progress: object | None, total: int = 0) -> None:
    if progress is None:
        return
    reset = getattr(progress, "reset", None)
    if callable(reset):
        try:
            reset(total)
            return
        except Exception:
            pass
    _progress_set_total(progress, total)


def _clear_recovered_build_manifests(
    root: Path,
    _modules: Sequence[Mapping[str, Any]],
) -> None:
    """Clear only transaction outcomes proven by their recorded byte hashes."""

    _recover_atomic_write_manifests(root)


def _reserved_bindings(module: Mapping[str, Any]) -> tuple[str, ...]:
    explicit = module.get("reservedBindings")
    if isinstance(explicit, list) and all(isinstance(item, str) for item in explicit):
        return tuple(explicit)
    symbols = module.get("symbols", [])
    names: list[str] = []
    if isinstance(symbols, list):
        for symbol in symbols:
            if isinstance(symbol, Mapping) and isinstance(symbol.get("name"), str):
                names.append(f"__jaunt_impl_{symbol['name']}")
    return tuple(names)


def _dependency_module_ids(module: Mapping[str, Any]) -> tuple[str, ...]:
    dependencies = module.get("dependencies", [])
    if not isinstance(dependencies, list):
        return ()
    module_id = _module_id(module)
    return tuple(
        sorted(
            {
                dependency.split("#", 1)[0]
                for dependency in dependencies
                if isinstance(dependency, str)
                and dependency.startswith("ts:")
                and dependency.split("#", 1)[0] != module_id
            }
        )
    )


def _topological_modules(
    modules: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    by_id = {_module_id(module): module for module in modules}
    visiting: list[str] = []
    visited: set[str] = set()
    ordered: list[Mapping[str, Any]] = []

    def visit(module_id: str) -> None:
        if module_id in visited:
            return
        if module_id in visiting:
            cycle = visiting[visiting.index(module_id) :] + [module_id]
            raise JauntConfigError("TypeScript dependency cycle: " + " -> ".join(cycle))
        visiting.append(module_id)
        module = by_id[module_id]
        for dependency in _dependency_module_ids(module):
            if dependency in by_id:
                visit(dependency)
        visiting.pop()
        visited.add(module_id)
        ordered.append(module)

    for module_id in sorted(by_id):
        visit(module_id)
    return tuple(ordered)


def _build_units(
    _analysis: TypeScriptAnalysis,
    modules: Sequence[Mapping[str, Any]],
) -> tuple[_BuildUnit, ...]:
    """Group only dependency-connected writes into atomic transactions.

    A shared package owner or project-reference graph is a validation scope,
    not a reason to discard unrelated successful candidates. Explicit Jaunt
    dependency edges still keep candidates that must move together in one unit.
    """

    by_id = {_module_id(module): module for module in modules}
    if not by_id:
        return ()

    module_parent = {module_id: module_id for module_id in by_id}

    def module_find(value: str) -> str:
        while module_parent[value] != value:
            module_parent[value] = module_parent[module_parent[value]]
            value = module_parent[value]
        return value

    def module_union(left: str, right: str) -> None:
        left_root = module_find(left)
        right_root = module_find(right)
        if left_root != right_root:
            first, second = sorted((left_root, right_root))
            module_parent[second] = first

    for module_id, module in by_id.items():
        for dependency in _dependency_module_ids(module):
            if dependency in by_id:
                module_union(module_id, dependency)

    grouped: dict[str, list[str]] = {}
    for module_id in sorted(by_id):
        grouped.setdefault(module_find(module_id), []).append(module_id)
    return tuple(
        _BuildUnit(key=module_ids[0], module_ids=tuple(module_ids))
        for module_ids in sorted(grouped.values(), key=lambda values: values[0])
    )


def _dependency_failure(module_id: str, dependencies: Sequence[str]) -> TargetDiagnostic:
    rendered = ", ".join(sorted(dependencies))
    return TargetDiagnostic(
        code="JAUNT_TS_DEPENDENCY_FAILED",
        message=(
            f"Skipped {module_id} because TypeScript dependency generation failed: {rendered}"
        ),
        data={"dependencies": tuple(sorted(dependencies))},
    )


def _component_failure(module_id: str, failed_ids: Sequence[str]) -> TargetDiagnostic:
    rendered = ", ".join(sorted(failed_ids))
    return TargetDiagnostic(
        code="JAUNT_TS_COMPONENT_ABORTED",
        message=(
            f"No artifacts were written for {module_id} because its dependency-connected "
            f"component failed: {rendered}"
        ),
        data={"failed_modules": tuple(sorted(failed_ids))},
    )


def _runtime_dependency_specifier(from_path: str, facade_path: str) -> str:
    emitted = str(Path(facade_path).with_suffix(".js")).replace("\\", "/")
    relative = posixpath.relpath(emitted, posixpath.dirname(from_path))
    return relative if relative.startswith(".") else f"./{relative}"


def _authored_dependency_specifier(module: Mapping[str, Any], dependency_id: str) -> str | None:
    """Reuse the spec's project-aware import for the generated public facade.

    Across project references, a filesystem-relative import into another
    project's source tree bypasses TypeScript's reference redirect and violates
    the consumer's rootDir. Authored path/package aliases already encode the
    project boundary, so retain them while removing the private ``.jaunt``
    segment. Relative imports are also safe here: IR renders them from the API
    mirror directory, which is the implementation directory too.
    """

    dependencies = module.get("dependencies", [])
    symbols = {
        dependency.split("#", 1)[1]
        for dependency in dependencies
        if isinstance(dependency, str)
        and dependency.startswith(f"{dependency_id}#")
        and "#" in dependency
    }
    imports = module.get("typeImports", [])
    if not isinstance(imports, list):
        return None
    for item in imports:
        if not isinstance(item, Mapping):
            continue
        bindings = item.get("namedImports", [])
        imported = {
            str(binding.get("imported"))
            for binding in bindings
            if isinstance(binding, Mapping) and isinstance(binding.get("imported"), str)
        }
        specifier = item.get("specifier")
        if symbols.intersection(imported) and isinstance(specifier, str):
            return re.sub(r"\.jaunt(?=(?:\.(?:js|jsx|ts|tsx))?$)", "", specifier)
    return None


def _build_request(
    root: Path,
    config: JauntConfig,
    module: Mapping[str, Any],
    dependency_modules: Mapping[str, Mapping[str, Any]],
    validator: Callable[[str], Awaitable[list[str]]],
    *,
    build_instructions: Sequence[str] = (),
    ephemeral_prompt: str = "",
    repo_map_block: str = "",
    project_overview_block: str = "",
    builtin_skill_names: Sequence[str] | None = None,
) -> GenerationRequest:
    module_id = _module_id(module)
    target_path = _module_path(module, "implementationPath")
    bindings = _reserved_bindings(module)
    system = _prompt_text(config.typescript_prompts.build_system, "build_system.md")
    user = _prompt_text(config.typescript_prompts.build_module, "build_module.md")
    user = (
        user.replace("{{target_path}}", target_path)
        .replace("{{reserved_bindings}}", ", ".join(bindings))
        .replace("{{module_kind}}", str(module.get("moduleKind", "project default")))
        .replace("{{module_resolution}}", str(module.get("moduleResolution", "project default")))
    )
    if build_instructions:
        user += "\n\nAdditional project instructions:\n" + "\n".join(
            f"- {instruction}" for instruction in build_instructions
        )
    context: dict[str, str] = {
        "_context/contract.json": json.dumps(
            _model_contract(module), sort_keys=True, indent=2, default=str
        )
        + "\n",
        "_context/spec.ts": str(module.get("specSource", "")),
        "_context/api.ts": str(module.get("apiSource", "")),
    }
    authored_context_source, _imported_type_context = _split_context_source(
        module.get("contextSource")
    )
    if authored_context_source is not None:
        context["_context/context.ts"] = authored_context_source
    dependency_context: list[dict[str, Any]] = []
    for dependency_id in _dependency_module_ids(module):
        other = dependency_modules.get(dependency_id)
        if other is None:
            raise JauntConfigError(
                f"{module_id} names unknown TypeScript dependency module {dependency_id}"
            )
        facade_path = _module_path(other, "facadePath")
        specifier = _authored_dependency_specifier(module, dependency_id) or (
            _runtime_dependency_specifier(target_path, facade_path)
        )
        index = len(dependency_context)
        context[f"_context/dependency_{index}.api.ts"] = str(other.get("apiSource", ""))
        dependency_context.append(
            {
                "moduleId": dependency_id,
                "facadePath": facade_path,
                "facadeSpecifier": specifier,
                "apiDigest": other.get("apiDigest"),
                "symbols": [
                    dependency
                    for dependency in module.get("dependencies", [])
                    if isinstance(dependency, str) and dependency.split("#", 1)[0] == dependency_id
                ],
            }
        )
    if dependency_context:
        context["_context/dependencies.json"] = (
            json.dumps(dependency_context, sort_keys=True, indent=2, default=str) + "\n"
        )
        user += "\n\nDeclared Jaunt dependency facades:\n" + "\n".join(
            f"- {item['moduleId']}: `{item['facadeSpecifier']}`" for item in dependency_context
        )
    if repo_map_block:
        context["_context/repository-map.md"] = repo_map_block
        user += "\n\nUse `_context/repository-map.md` for bounded repository orientation."
    if project_overview_block:
        context["_context/project-overview.md"] = project_overview_block
        user += "\n\nUse `_context/project-overview.md` as project-level architectural context."
    if ephemeral_prompt:
        user += "\n\nEphemeral validation feedback for this invocation only:\n" + ephemeral_prompt
    return GenerationRequest(
        language="ts",
        kind="build",
        target_path=target_path,
        context_files=context,
        prompt=f"{system}\n\n{user}",
        cache_payload={
            "moduleId": module_id,
            "structuralDigest": module.get("structuralDigest"),
            "proseDigest": module.get("proseDigest"),
            "apiDigest": module.get("apiDigest"),
            "bindings": bindings,
        },
        validator=validator,
        project_root=root,
        builtin_skill_names=(
            tuple(builtin_skill_names)
            if builtin_skill_names is not None
            else (tuple(config.skills.builtin_skills) if config.skills.builtin else ())
        ),
    )


def _default_backend(config: JauntConfig) -> GeneratorBackend:
    if config.codex.model != "gpt-5.6-sol":
        raise JauntConfigError('TypeScript generation requires [codex].model = "gpt-5.6-sol"')
    return CodexBackend(config.codex, config.llm, config.prompts)


async def _gate_prose_change(
    root: Path,
    module: Mapping[str, Any],
    config: JauntConfig,
    *,
    cost: CostTracker | None = None,
    run_exec: Callable[..., Awaitable[Any]] | None = None,
) -> bool:
    """Return true only when the judge confidently finds prose equivalent."""

    sidecar_path = _safe_path(root, _module_path(module, "sidecarPath"))
    try:
        old = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(old, Mapping):
        return False
    old_symbols = old.get("symbols", [])
    new_symbols = module.get("symbols", [])
    if not isinstance(old_symbols, list) or not isinstance(new_symbols, list):
        return False
    previous = {
        str(symbol.get("name")): symbol
        for symbol in old_symbols
        if isinstance(symbol, Mapping) and isinstance(symbol.get("name"), str)
    }
    judged_change = False

    async def equivalent(prompt: str) -> bool:
        try:
            with tempfile.TemporaryDirectory() as temporary:
                exec_fn = run_exec or run_codex_exec
                result = await exec_fn(
                    prompt=prompt,
                    cwd=temporary,
                    sandbox="read-only",
                    model=config.semantic_gate.model,
                    reasoning_effort=config.semantic_gate.reasoning_effort,
                    ignore_user_config=True,
                )
        except Exception:
            return False
        usage_input = getattr(result, "usage_input", None)
        usage_output = getattr(result, "usage_output", None)
        if cost is not None and isinstance(usage_input, int) and isinstance(usage_output, int):
            cost.record(
                f"{_module_id(module)}:semantic-gate",
                TokenUsage(
                    prompt_tokens=usage_input,
                    completion_tokens=usage_output,
                    model=config.semantic_gate.model,
                    provider="codex",
                    cached_prompt_tokens=getattr(result, "usage_cached", None) or 0,
                ),
            )
            cost.check_budget()
        return result.final_message.strip() == "EQUIVALENT"

    for symbol in new_symbols:
        if not isinstance(symbol, Mapping) or not isinstance(symbol.get("name"), str):
            return False
        before = previous.get(str(symbol["name"]))
        if before is None:
            return False
        old_docs = str(before.get("docs", ""))
        new_docs = str(symbol.get("docs", ""))
        if old_docs == new_docs:
            continue
        judged_change = True
        structural_contract = {str(key): value for key, value in symbol.items() if key != "docs"}
        prompt = (
            "A TypeScript declaration's TSDoc is its behavioral contract. Its type/"
            "signature IR is unchanged. Determine whether NEW requires, forbids, or "
            "relaxes any behavior compared with OLD (including results, errors, ordering, "
            "edge cases, async behavior, or complexity). Reply exactly EQUIVALENT or "
            "MEANINGFUL; uncertainty is MEANINGFUL.\n\n"
            f"DECLARATION IR:\n{json.dumps(structural_contract, sort_keys=True)}\n\n"
            f"OLD:\n{old_docs}\n\nNEW:\n{new_docs}"
        )
        if not await equivalent(prompt):
            return False

    old_context_docs = old.get("contextDocs")
    new_context_docs = module.get("contextDocs")
    if old_context_docs != new_context_docs:
        # An old sidecar without the canonical records cannot establish what
        # prose changed. Rebuild once instead of guessing from digests alone.
        if not isinstance(old_context_docs, list) or not isinstance(new_context_docs, list):
            return False
        judged_change = True
        prompt = (
            "These are TSDoc contracts imported into a generated TypeScript module's "
            "type/runtime context. The structural type environment is unchanged. "
            "Determine whether NEW requires, forbids, or relaxes any behavior compared "
            "with OLD (including results, errors, ordering, edge cases, async behavior, "
            "or complexity). Reply exactly EQUIVALENT or MEANINGFUL; uncertainty is "
            "MEANINGFUL.\n\n"
            f"OLD CONTEXT DOCS:\n{json.dumps(old_context_docs, sort_keys=True)}\n\n"
            f"NEW CONTEXT DOCS:\n{json.dumps(new_context_docs, sort_keys=True)}"
        )
        if not await equivalent(prompt):
            return False

    # A changed prose digest without an inspectable doc delta is not evidence
    # of equivalence. Failing closed here prevents automatic restamps.
    return judged_change


async def run_build_in_session(
    root: Path,
    config: JauntConfig,
    client: WorkerLike,
    initialized: InitializeResult,
    *,
    target_ids: Sequence[str] = (),
    force: bool = False,
    generator: GeneratorBackend | None = None,
    cost_tracker: CostTracker | None = None,
    response_cache: ResponseCache | None = None,
    progress: object | None = None,
    finish_progress: bool = True,
    jobs: int | None = None,
    max_attempts: int = _DEFAULT_ATTEMPTS,
    semantic_gate_enabled: bool | None = None,
    semantic_gate_exec: Callable[..., Awaitable[Any]] | None = None,
    build_instructions: Sequence[str] | None = None,
    ephemeral_prompt: str = "",
    repo_map_block: str | None = None,
    project_overview_enabled: bool | None = None,
    builtin_skill_names: Sequence[str] | None = None,
    generation_fingerprint: str | None = None,
    reuse_proof_sink: dict[str, dict[str, str]] | None = None,
    validate_committed_batteries: bool = True,
) -> TargetBuildReport:
    """Build against an already initialized analyzer session.

    Watch mode uses this entry point so worker caches survive across cycles.
    Normal callers should use :func:`run_build`.
    """

    from jaunt.typescript.status import classify_modules

    root = root.resolve()
    if response_cache is None:
        response_cache = ResponseCache(root / ".jaunt" / "cache")
    effective_jobs = config.build.jobs if jobs is None else jobs
    if effective_jobs < 1:
        raise JauntConfigError("TypeScript build jobs must be >= 1")
    backend = generator or _default_backend(config)
    cost = cost_tracker or CostTracker(max_cost=config.llm.max_cost_per_build)
    effective_builtin_skills = (
        tuple(builtin_skill_names)
        if builtin_skill_names is not None
        else (tuple(config.skills.builtin_skills) if config.skills.builtin else ())
    )
    overview_enabled = (
        bool(config.context.overview)
        if project_overview_enabled is None
        else project_overview_enabled
    )
    repo_map_is_enabled = (
        bool(config.context.repo_map) if repo_map_block is None else bool(repo_map_block)
    )
    if repo_map_block is None:
        repo_map_block = ""
        if repo_map_is_enabled:
            from jaunt.repo_context.api import repo_map_block_for_build

            repo_map_block = repo_map_block_for_build(
                root=root,
                cfg=config,
                today=date.today().isoformat(),
            )
    advisories: dict[str, tuple[str, ...]] = {}
    candidates: dict[str, str] = {}
    generated: set[str] = set()
    skipped: set[str] = set()
    refrozen: set[str] = set()
    recomposed: set[str] = set()
    effective_instructions = (
        tuple(build_instructions)
        if build_instructions is not None
        else tuple(config.build.instructions)
    )
    request_fingerprint = generation_fingerprint or _generation_fingerprint(
        config,
        root=root,
        build_instructions=effective_instructions,
        builtin_skill_names=effective_builtin_skills,
        repo_map_enabled=repo_map_is_enabled,
        project_overview_enabled=overview_enabled,
    )
    analysis = await analyze(client, initialized, target_ids=target_ids)
    previous_target_api_records = capture_target_api_records(root, analysis.modules)
    pending_designs = [
        _module_id(module)
        for module in analysis.modules
        if "@jauntDesign" in str(module.get("specSource", ""))
    ]
    if pending_designs:
        raise JauntConfigError(
            "TypeScript declarations still require reviewable design; run "
            "`jaunt design --target <module#symbol>` first: " + ", ".join(sorted(pending_designs))
        )
    output_preconditions = _artifact_preconditions(root, analysis.modules)
    status = classify_modules(root, analysis.modules)
    gate_enabled = (
        config.semantic_gate.enabled if semantic_gate_enabled is None else semantic_gate_enabled
    )
    for module in analysis.modules:
        module_id = _module_id(module)
        reason = status.stale.get(module_id)
        if not force and reason == "fingerprint":
            refrozen.add(module_id)
        elif not force and reason == "toolchain":
            refrozen.add(module_id)
            recomposed.add(module_id)
        elif not force and reason == "prose" and gate_enabled:
            if await _gate_prose_change(
                root,
                module,
                config,
                cost=cost,
                run_exec=semantic_gate_exec,
            ):
                refrozen.add(module_id)
        elif not force and module_id in status.invalid:
            invalid_codes = {item.code for item in status.invalid[module_id]}
            if invalid_codes and invalid_codes <= {
                "JAUNT_TS_API_DRIFT",
                "JAUNT_TS_FACADE_DRIFT",
            }:
                refrozen.add(module_id)
    selected = [
        module
        for module in analysis.modules
        if force
        or (_module_id(module) in status.stale and _module_id(module) not in refrozen)
        or _module_id(module) in status.unbuilt
        or (_module_id(module) in status.invalid and _module_id(module) not in refrozen)
    ]
    # Validate the selected dependency graph before launching any model work.
    selected = list(_topological_modules(selected))
    selected_ids = {_module_id(module) for module in selected}
    skipped.update(
        _module_id(module)
        for module in analysis.modules
        if _module_id(module) not in selected_ids and _module_id(module) not in refrozen
    )
    by_id = {_module_id(module): module for module in analysis.modules}

    if not selected and not refrozen:
        _clear_recovered_build_manifests(root, analysis.modules)
        if finish_progress:
            _progress_finish(progress)
        return TargetBuildReport(
            language="ts",
            skipped=frozenset(skipped),
            metadata={
                "cost": cost.summary_dict(),
                **(
                    {"cache": {"hits": response_cache.hits, "misses": response_cache.misses}}
                    if response_cache is not None
                    else {}
                ),
            },
        )

    actionable_ids = selected_ids | refrozen
    _progress_set_total(progress, len(actionable_ids))
    actionable_modules = [by_id[module_id] for module_id in sorted(actionable_ids)]
    units = _build_units(analysis, actionable_modules)
    failed: dict[str, tuple[TargetDiagnostic, ...]] = {}
    requests: dict[str, GenerationRequest] = {}
    candidate_attempts: dict[str, int] = {}
    candidate_retries: dict[str, int] = {}
    candidate_retry_reasons: dict[str, list[str]] = {}
    candidate_infrastructure_retries: dict[str, int] = {}
    candidate_infrastructure_errors: dict[str, list[str]] = {}
    pending = set(selected_ids)
    semaphore = asyncio.Semaphore(effective_jobs)
    progress_advanced: set[str] = set()
    project_overview_block = (
        await _project_overview_block(
            root,
            config,
            analysis.modules,
            repo_map_block=repo_map_block,
            backend=backend,
            cost_tracker=cost,
            enabled=overview_enabled,
        )
        if selected
        else ""
    )

    def selected_dependencies(module_id: str) -> set[str]:
        return set(_dependency_module_ids(by_id[module_id])).intersection(selected_ids)

    def candidate_dependencies(module_id: str) -> dict[str, str]:
        found: dict[str, str] = {}
        stack = list(selected_dependencies(module_id))
        while stack:
            dependency = stack.pop()
            if dependency in found:
                continue
            source = candidates.get(dependency)
            if source is not None:
                found[dependency] = source
            stack.extend(selected_dependencies(dependency))
        return found

    async def generate_one(
        module_id: str,
    ) -> tuple[
        str,
        GenerationRequest,
        GenerationResult,
        tuple[str, ...],
    ]:
        module = by_id[module_id]

        async def candidate_validator(source: str) -> list[str]:
            proposed = {**candidate_dependencies(module_id), module_id: source}
            validation = await validate_overlay(
                client,
                analysis,
                proposed,
                tuple(sorted(proposed)),
                scoped_validation=True,
            )
            if validation.valid:
                if validate_committed_batteries:
                    from jaunt.typescript.tester import _validate_committed_target_batteries

                    try:
                        return await _validate_committed_target_batteries(
                            client,
                            initialized,
                            root,
                            config,
                            analysis,
                            module_ids=(module_id,),
                            artifact_overlays={
                                write.path: write.content or ""
                                for write in _artifact_writes(validation)
                            },
                        )
                    except _CommittedBatteryInfrastructureError as error:
                        error.attach_candidate(source)
                        raise
                return []
            return [
                f"{diagnostic.code}: {diagnostic.message}"
                + (f" ({diagnostic.path})" if diagnostic.path else "")
                for diagnostic in validation.diagnostics
            ] or ["TypeScript overlay validation failed"]

        request = _build_request(
            root,
            config,
            module,
            by_id,
            candidate_validator,
            build_instructions=effective_instructions,
            ephemeral_prompt=ephemeral_prompt,
            repo_map_block=repo_map_block,
            project_overview_block=project_overview_block,
            builtin_skill_names=effective_builtin_skills,
        )
        async with semaphore:
            _progress_phase(progress, module_id, "generating")
            attempt_count = 0
            attempt_usage: list[TokenUsage] = []
            cached_source_failed_infrastructure = False

            def request_progress(stage: str, detail: str) -> None:
                nonlocal attempt_count
                if stage == "attempt":
                    attempt_count += 1
                _progress_phase(progress, module_id, stage, detail)

            def record_request_usage(usage: TokenUsage) -> None:
                attempt_usage.append(usage)
                cost.record(module_id, usage)
                cost.check_budget()

            async def validate_cached_source(source: str) -> list[str]:
                nonlocal cached_source_failed_infrastructure
                try:
                    return await candidate_validator(source)
                except _CommittedBatteryInfrastructureError:
                    cached_source_failed_infrastructure = True
                    raise

            try:
                result = await generate_request_cached(
                    backend,
                    request,
                    max_attempts=max_attempts,
                    generation_fingerprint=request_fingerprint,
                    response_cache=(None if force else response_cache),
                    cost_tracker=cost,
                    usage_callback=record_request_usage,
                    usage_label=module_id,
                    progress=request_progress,
                    cached_validator=validate_cached_source,
                )
            except _CommittedBatteryInfrastructureError as error:
                # The candidate may have been generated once, but a runner outage
                # must never be presented to Codex as implementation feedback or
                # consume the remaining candidate-attempt budget.
                usage = (
                    TokenUsage(
                        prompt_tokens=sum(item.prompt_tokens for item in attempt_usage),
                        completion_tokens=sum(item.completion_tokens for item in attempt_usage),
                        model=attempt_usage[-1].model,
                        provider=attempt_usage[-1].provider,
                        cached_prompt_tokens=sum(
                            item.cached_prompt_tokens for item in attempt_usage
                        ),
                    )
                    if attempt_usage
                    else None
                )
                result = GenerationResult(
                    attempts=0 if cached_source_failed_infrastructure else max(1, attempt_count),
                    source=error.candidate_source,
                    errors=list(error.errors),
                    usage=usage,
                )
                if result.source is not None and result.attempts > 0:
                    store_generation_result(
                        response_cache,
                        backend,
                        request,
                        replace(result, errors=[]),
                        generation_fingerprint=request_fingerprint,
                    )
                return module_id, request, result, error.errors
        return module_id, request, result, ()

    while pending:
        propagated = False
        for module_id in sorted(tuple(pending)):
            blocked = sorted(selected_dependencies(module_id).intersection(failed))
            if not blocked:
                continue
            failed[module_id] = (_dependency_failure(module_id, blocked),)
            pending.remove(module_id)
            _progress_advance(progress, module_id, ok=False)
            progress_advanced.add(module_id)
            propagated = True
        if propagated:
            continue
        ready = sorted(
            module_id
            for module_id in pending
            if not selected_dependencies(module_id).intersection(pending)
        )
        if not ready:
            # _topological_modules already reports a precise cycle. This is a
            # defensive guard against malformed dependency data changing mid-run.
            raise JauntConfigError("TypeScript dependency scheduler made no progress")
        results = await asyncio.gather(*(generate_one(module_id) for module_id in ready))
        for module_id, request, result, battery_infrastructure_errors in results:
            pending.remove(module_id)
            requests[module_id] = request
            candidate_attempts[module_id] = result.attempts
            candidate_retries[module_id] = max(0, result.attempts - 1)
            candidate_retry_reasons[module_id] = [
                error for attempt in result.attempt_errors for error in attempt
            ]
            candidate_infrastructure_retries[module_id] = result.infrastructure_retries
            candidate_infrastructure_errors[module_id] = list(result.infrastructure_errors)
            if result.source is None or result.errors:
                failed[module_id] = tuple(
                    TargetDiagnostic(
                        code=(
                            "JAUNT_TS_COMMITTED_BATTERY_INFRASTRUCTURE"
                            if battery_infrastructure_errors
                            else (
                                "JAUNT_TS_GENERATION_INFRASTRUCTURE"
                                if result.infrastructure_exhausted
                                else "JAUNT_TS_GENERATION"
                            )
                        ),
                        message=error,
                    )
                    for error in result.errors or ["The generator returned no TypeScript source"]
                )
                _progress_advance(progress, module_id, ok=False)
                progress_advanced.add(module_id)
                continue
            candidates[module_id] = result.source
            if result.advisories:
                advisories[module_id] = result.advisories

    # A component is an atomic write boundary. Successful siblings of a failed
    # module are reported as aborted rather than silently appearing generated.
    for unit in units:
        failed_in_unit = sorted(set(unit.module_ids).intersection(failed))
        if not failed_in_unit:
            continue
        for module_id in unit.module_ids:
            if module_id in actionable_ids and module_id not in failed:
                failed[module_id] = (_component_failure(module_id, failed_in_unit),)
            candidates.pop(module_id, None)

    changing_artifacts = {
        _module_path(by_id[module_id], key)
        for module_id in actionable_ids
        for key in ("facadePath", "apiMirrorPath", "implementationPath", "sidecarPath")
    }
    immutable_inputs = {
        path: digest
        for path, digest in _input_hashes(analysis.contracts).items()
        if path not in changing_artifacts
    }
    writes: list[_Write] = []
    committed_refrozen: set[str] = set()
    for index, unit in enumerate(units):
        unit_ids = set(unit.module_ids).intersection(actionable_ids)
        if unit_ids.intersection(failed):
            continue
        unit_candidates = {
            module_id: candidates[module_id]
            for module_id in sorted(unit_ids.intersection(candidates))
        }
        unit_refrozen = tuple(sorted(unit_ids.intersection(refrozen)))
        unit_recomposed = tuple(sorted(unit_ids.intersection(recomposed)))
        unit_restamped = tuple(sorted(set(unit_refrozen) - set(unit_recomposed)))

        async def validate_unit(
            proposed: Mapping[str, str],
            *,
            active_analysis: TypeScriptAnalysis = analysis,
            refrozen_ids: tuple[str, ...] = unit_refrozen,
            restamped_ids: tuple[str, ...] = unit_restamped,
            recomposed_ids: tuple[str, ...] = unit_recomposed,
        ) -> ValidateOverlayResult:
            return await validate_overlay(
                client,
                active_analysis,
                proposed,
                tuple(sorted(set(proposed) | set(refrozen_ids))),
                restamp_module_ids=restamped_ids,
                recompose_module_ids=recomposed_ids,
                scoped_validation=True,
                baseline_unselected=True,
            )

        unit_battery_proof: dict[str, Any] = {}

        async def final_battery_errors(
            result: ValidateOverlayResult,
            *,
            active_analysis: TypeScriptAnalysis = analysis,
            active_candidates: dict[str, str] = unit_candidates,
            battery_proof: dict[str, Any] = unit_battery_proof,
        ) -> list[str]:
            battery_proof.clear()
            if not validate_committed_batteries or not result.valid:
                return []
            from jaunt.typescript.tester import _validate_committed_target_batteries

            return await _validate_committed_target_batteries(
                client,
                initialized,
                root,
                config,
                active_analysis,
                module_ids=tuple(sorted(active_candidates)),
                artifact_overlays={
                    write.path: write.content or "" for write in _artifact_writes(result)
                },
                proof_sink=battery_proof,
            )

        validated = await validate_unit(unit_candidates)
        battery_infrastructure_errors: tuple[str, ...] = ()
        try:
            battery_errors = await final_battery_errors(validated)
        except _CommittedBatteryInfrastructureError as error:
            battery_errors = []
            battery_infrastructure_errors = error.errors
        while (not validated.valid or battery_errors) and not battery_infrastructure_errors:
            diagnostics = (
                _diagnostics(validated.diagnostics)
                if not validated.valid
                else tuple(
                    TargetDiagnostic(
                        code="JAUNT_TS_COMMITTED_BATTERY",
                        message=error,
                    )
                    for error in battery_errors
                )
            )
            implementation_owners = {
                _module_path(by_id[module_id], "implementationPath"): module_id
                for module_id in unit_candidates
            }
            implicated = [
                implementation_owners[diagnostic.path]
                for diagnostic in diagnostics
                if diagnostic.path in implementation_owners
            ]
            repairable = next(
                (
                    module_id
                    for module_id in dict.fromkeys([*implicated, *sorted(unit_candidates)])
                    if candidate_attempts.get(module_id, 0) < max_attempts
                ),
                None,
            )
            if repairable is None:
                break
            owned_diagnostics = [
                diagnostic
                for diagnostic in diagnostics
                if diagnostic.path in {None, _module_path(by_id[repairable], "implementationPath")}
            ] or list(diagnostics)
            reasons = [
                f"{diagnostic.code}: {diagnostic.message}"
                + (f" ({diagnostic.path})" if diagnostic.path else "")
                for diagnostic in owned_diagnostics
            ]
            candidate_retry_reasons.setdefault(repairable, []).extend(reasons)

            async def unit_candidate_validator(
                source: str,
                *,
                module_id: str = repairable,
                current_candidates: dict[str, str] = unit_candidates,
                active_analysis: TypeScriptAnalysis = analysis,
            ) -> list[str]:
                proposed = {**current_candidates, module_id: source}
                result = await validate_unit(proposed)
                if result.valid:
                    if validate_committed_batteries:
                        from jaunt.typescript.tester import _validate_committed_target_batteries

                        try:
                            return await _validate_committed_target_batteries(
                                client,
                                initialized,
                                root,
                                config,
                                active_analysis,
                                module_ids=(module_id,),
                                artifact_overlays={
                                    write.path: write.content or ""
                                    for write in _artifact_writes(result)
                                },
                            )
                        except _CommittedBatteryInfrastructureError as infrastructure_error:
                            raise infrastructure_error.attach_candidate(source) from None
                    return []
                return [
                    f"{diagnostic.code}: {diagnostic.message}"
                    + (f" ({diagnostic.path})" if diagnostic.path else "")
                    for diagnostic in _diagnostics(result.diagnostics)
                ] or ["TypeScript unit overlay validation failed"]

            repair_request = replace(
                requests[repairable],
                seed_target_content=unit_candidates[repairable],
                validator=unit_candidate_validator,
            )
            _progress_phase(
                progress,
                repairable,
                "retrying final conformance",
                reasons[0] if reasons else "unit overlay failed",
            )
            async with semaphore:
                repair_attempt_count = 0
                repair_usage: list[TokenUsage] = []

                def record_repair_usage(
                    usage: TokenUsage,
                    item: str = repairable,
                    usages: list[TokenUsage] = repair_usage,
                ) -> None:
                    usages.append(usage)
                    cost.record(item, usage)
                    cost.check_budget()

                def repair_progress(
                    stage: str,
                    detail: str,
                    item: str = repairable,
                ) -> None:
                    nonlocal repair_attempt_count
                    if stage == "attempt":
                        repair_attempt_count += 1
                    _progress_phase(progress, item, stage, detail)

                try:
                    repaired = await backend.generate_request_with_retry(
                        repair_request,
                        max_attempts=1,
                        initial_error_context=[
                            f"previous output errors: {reason}" for reason in reasons
                        ],
                        progress=repair_progress,
                        usage_callback=record_repair_usage,
                    )
                except _CommittedBatteryInfrastructureError as error:
                    battery_infrastructure_errors = error.errors
                    paid_attempts = max(1, repair_attempt_count, len(repair_usage))
                    candidate_attempts[repairable] = (
                        candidate_attempts.get(repairable, 0) + paid_attempts
                    )
                    candidate_retries[repairable] = (
                        candidate_retries.get(repairable, 0) + paid_attempts
                    )
                    if error.candidate_source is not None:
                        unit_candidates[repairable] = error.candidate_source
                        candidates[repairable] = error.candidate_source
                        validated = await validate_unit(unit_candidates)
                    break
            candidate_attempts[repairable] = (
                candidate_attempts.get(repairable, 0) + repaired.attempts
            )
            candidate_retries[repairable] = candidate_retries.get(repairable, 0) + 1
            candidate_infrastructure_retries[repairable] = (
                candidate_infrastructure_retries.get(repairable, 0)
                + repaired.infrastructure_retries
            )
            candidate_infrastructure_errors.setdefault(repairable, []).extend(
                repaired.infrastructure_errors
            )
            candidate_retry_reasons[repairable].extend(
                error for attempt in repaired.attempt_errors for error in attempt
            )
            if repaired.source is None:
                break
            unit_candidates[repairable] = repaired.source
            candidates[repairable] = repaired.source
            if repaired.advisories:
                advisories[repairable] = repaired.advisories
            validated = await validate_unit(unit_candidates)
            try:
                battery_errors = await final_battery_errors(validated)
            except _CommittedBatteryInfrastructureError as error:
                battery_errors = []
                battery_infrastructure_errors = error.errors
        if not validated.valid or battery_errors or battery_infrastructure_errors:
            if battery_infrastructure_errors:
                if validated.valid:
                    for module_id in sorted(unit_candidates):
                        store_generation_result(
                            response_cache,
                            backend,
                            requests[module_id],
                            GenerationResult(
                                attempts=candidate_attempts.get(module_id, 0),
                                source=unit_candidates[module_id],
                                errors=[],
                                advisories=advisories.get(module_id, ()),
                            ),
                            generation_fingerprint=request_fingerprint,
                        )
                diagnostics = tuple(
                    TargetDiagnostic(
                        code="JAUNT_TS_COMMITTED_BATTERY_INFRASTRUCTURE",
                        message=error,
                    )
                    for error in battery_infrastructure_errors
                )
            elif not validated.valid:
                diagnostics = _diagnostics(validated.diagnostics)
            else:
                diagnostics = tuple(
                    TargetDiagnostic(
                        code="JAUNT_TS_COMMITTED_BATTERY",
                        message=error,
                    )
                    for error in battery_errors
                )
            for module_id in unit_ids:
                failed[module_id] = diagnostics
            continue
        unit_artifact_paths = {
            _module_path(by_id[module_id], key)
            for module_id in unit_ids
            for key in ("facadePath", "apiMirrorPath", "implementationPath", "sidecarPath")
        }
        unit_expected_inputs = {
            **immutable_inputs,
            **{path: output_preconditions[path] for path in sorted(unit_artifact_paths)},
        }
        raw_battery_preconditions = unit_battery_proof.get("preconditions", {})
        if not isinstance(raw_battery_preconditions, Mapping):
            raise JauntGenerationError("Committed battery validation returned an invalid proof")
        for path, digest in raw_battery_preconditions.items():
            if not isinstance(path, str) or not isinstance(digest, str):
                raise JauntGenerationError("Committed battery validation returned an invalid proof")
            previous = unit_expected_inputs.setdefault(path, digest)
            if previous != digest:
                raise JauntGenerationError(
                    "Committed battery validation conflicts with an analyzed input: " + path
                )

        def verify_unit_commit_environment(
            battery_proof: Mapping[str, Any] = unit_battery_proof,
        ) -> None:
            if not battery_proof:
                _verify_worker_runtime_identity(client)
                return
            from jaunt.typescript.tester import _verify_test_commit_environment

            runner = battery_proof.get("runner_fingerprint")
            vitest_config = battery_proof.get("vitest_config")
            config_closure = battery_proof.get("config_closure")
            if (
                not isinstance(runner, str)
                or not isinstance(vitest_config, str)
                or not isinstance(config_closure, Mapping)
            ):
                raise JauntGenerationError("Committed battery validation returned an invalid proof")
            _verify_test_commit_environment(
                root,
                client,
                initialized,
                runner,
                vitest_config=vitest_config,
                config_closure=cast("Mapping[str, str]", config_closure),
            )

        def seal_unit_commit_environment(
            battery_proof: Mapping[str, Any] = unit_battery_proof,
        ) -> None:
            if not battery_proof:
                _seal_worker_runtime_identity(client)
                return
            from jaunt.typescript.tester import _seal_test_commit_environment

            runner = battery_proof.get("runner_fingerprint")
            vitest_config = battery_proof.get("vitest_config")
            config_closure = battery_proof.get("config_closure")
            if (
                not isinstance(runner, str)
                or not isinstance(vitest_config, str)
                or not isinstance(config_closure, Mapping)
            ):
                raise JauntGenerationError("Committed battery validation returned an invalid proof")
            _seal_test_commit_environment(
                root,
                client,
                initialized,
                runner,
                vitest_config=vitest_config,
                config_closure=cast("Mapping[str, str]", config_closure),
            )

        unit_writes = atomic_write_manifest(
            root,
            _artifact_writes(validated),
            expected_inputs=unit_expected_inputs,
            pre_commit_guard=verify_unit_commit_environment,
            commit_seal=seal_unit_commit_environment,
        )
        writes.extend(unit_writes)
        generated.update(unit_ids.intersection(candidates))
        committed_refrozen.update(unit_ids.intersection(refrozen))
        for module_id in sorted(unit_candidates):
            store_generation_result(
                None if force else response_cache,
                backend,
                requests[module_id],
                GenerationResult(
                    attempts=candidate_attempts.get(module_id, 0),
                    source=unit_candidates[module_id],
                    errors=[],
                    advisories=advisories.get(module_id, ()),
                ),
                generation_fingerprint=request_fingerprint,
            )
        later_units = units[index + 1 :]
        if any(
            set(later.module_ids).intersection(actionable_ids)
            and not set(later.module_ids).intersection(failed)
            for later in later_units
        ):
            await client.request(
                "invalidate",
                {"paths": sorted(write.path for write in unit_writes)},
            )
            analysis = await analyze(client, initialized, target_ids=target_ids)
    update_target_api_reuse_proof(
        root,
        before=previous_target_api_records,
        modules=analysis.modules,
        reused_module_ids=committed_refrozen,
        touched_module_ids=set(generated) | committed_refrozen,
    )
    if reuse_proof_sink is not None:
        reuse_proof_sink.clear()
        reuse_proof_sink.update(
            {
                module_id: dict(previous_target_api_records[module_id])
                for module_id in committed_refrozen.intersection(recomposed)
                if module_id in previous_target_api_records
            }
        )
    _clear_recovered_build_manifests(root, analysis.modules)
    for module_id in sorted(actionable_ids - progress_advanced):
        if module_id in committed_refrozen:
            _progress_phase(
                progress,
                module_id,
                "recomposed" if module_id in recomposed else "refrozen",
            )
        _progress_advance(progress, module_id, ok=module_id not in failed)
    if finish_progress:
        _progress_finish(progress)

    append_events(
        root,
        [
            JournalEvent("build", module_id, "TypeScript overlay validated")
            for module_id in sorted(generated)
        ]
        + [
            JournalEvent(
                "recompose" if module_id in recomposed else "refreeze",
                module_id,
                (
                    "TypeScript candidate recomposed for compatible toolchain"
                    if module_id in recomposed
                    else "TypeScript contract equivalent"
                ),
            )
            for module_id in sorted(committed_refrozen)
        ],
    )
    return TargetBuildReport(
        language="ts",
        generated=frozenset(generated),
        skipped=frozenset(skipped),
        refrozen=frozenset(committed_refrozen),
        failed=failed,
        advisories=advisories,
        metadata={
            "cost": cost.summary_dict(),
            "artifacts": tuple(write.path for write in writes),
            "build_units": tuple(unit.module_ids for unit in units),
            "jobs": effective_jobs,
            "generation_fingerprint": request_fingerprint,
            "recomposed": tuple(sorted(committed_refrozen.intersection(recomposed))),
            "candidate_outcomes": {
                module_id: {
                    "attempts": candidate_attempts.get(module_id, 0),
                    "retry_count": candidate_retries.get(module_id, 0),
                    "retry_reasons": tuple(
                        dict.fromkeys(candidate_retry_reasons.get(module_id, ()))
                    ),
                    "phase": "committed" if module_id in generated else "failed",
                    **(
                        {
                            "infrastructure_retries": candidate_infrastructure_retries.get(
                                module_id,
                                0,
                            ),
                            "infrastructure_errors": tuple(
                                candidate_infrastructure_errors.get(module_id, ())
                            ),
                        }
                        if candidate_infrastructure_errors.get(module_id)
                        else {}
                    ),
                }
                for module_id in sorted(candidate_attempts)
            },
            **(
                {"cache": {"hits": response_cache.hits, "misses": response_cache.misses}}
                if response_cache is not None
                else {}
            ),
        },
        exit_code=3 if failed else 0,
    )


async def run_build(
    root: Path,
    config: JauntConfig,
    *,
    target_ids: Sequence[str] = (),
    force: bool = False,
    generator: GeneratorBackend | None = None,
    cost_tracker: CostTracker | None = None,
    response_cache: ResponseCache | None = None,
    progress: object | None = None,
    finish_progress: bool = True,
    worker_factory: WorkerFactory | None = None,
    jobs: int | None = None,
    max_attempts: int = _DEFAULT_ATTEMPTS,
    semantic_gate_enabled: bool | None = None,
    semantic_gate_exec: Callable[..., Awaitable[Any]] | None = None,
    build_instructions: Sequence[str] | None = None,
    ephemeral_prompt: str = "",
    repo_map_enabled: bool | None = None,
    repo_map_block_override: str | None = None,
    auto_skills_enabled: bool | None = None,
    builtin_skill_names: Sequence[str] | None = None,
    reuse_proof_sink: dict[str, dict[str, str]] | None = None,
    validate_committed_batteries: bool = True,
) -> TargetBuildReport:
    """Generate reserved TypeScript bindings, validate overlays, and commit them."""

    root = root.resolve()
    effective_instructions = (
        tuple(build_instructions)
        if build_instructions is not None
        else tuple(config.build.instructions)
    )
    effective_builtin_skills = (
        tuple(builtin_skill_names)
        if builtin_skill_names is not None
        else (tuple(config.skills.builtin_skills) if config.skills.builtin else ())
    )
    use_repo_map = bool(config.context.repo_map) if repo_map_enabled is None else repo_map_enabled
    target = _target(config)
    use_auto_skills = (
        target.auto_skills_enabled(bool(config.skills.auto))
        if auto_skills_enabled is None
        else auto_skills_enabled
    )
    npm_skill_metadata: Mapping[str, object] = {}
    if use_auto_skills:
        from jaunt.skills_npm import ensure_npm_skills, typescript_package_owners

        npm_skills = ensure_npm_skills(
            project_root=root,
            package_owners=typescript_package_owners(root, target),
            max_readme_chars=config.skills.max_chars_per_skill,
        )
        npm_skill_metadata = npm_skills.metadata()

    repo_map_block = ""
    if use_repo_map and repo_map_block_override is not None:
        repo_map_block = repo_map_block_override
    elif use_repo_map:
        from jaunt.repo_context.api import repo_map_block_for_build

        repo_map_block = repo_map_block_for_build(
            root=root,
            cfg=config,
            today=date.today().isoformat(),
        )
    request_fingerprint = _generation_fingerprint(
        config,
        root=root,
        build_instructions=effective_instructions,
        builtin_skill_names=effective_builtin_skills,
        repo_map_enabled=use_repo_map,
        project_overview_enabled=bool(config.context.overview),
    )
    async with worker_session(
        root,
        config,
        worker_factory=worker_factory,
        generation_fingerprint=request_fingerprint,
    ) as (client, initialized):
        report = await run_build_in_session(
            root,
            config,
            client,
            initialized,
            target_ids=target_ids,
            force=force,
            generator=generator,
            cost_tracker=cost_tracker,
            response_cache=response_cache,
            progress=progress,
            finish_progress=finish_progress,
            jobs=jobs,
            max_attempts=max_attempts,
            semantic_gate_enabled=semantic_gate_enabled,
            semantic_gate_exec=semantic_gate_exec,
            build_instructions=effective_instructions,
            ephemeral_prompt=ephemeral_prompt,
            repo_map_block=repo_map_block,
            project_overview_enabled=bool(config.context.overview),
            builtin_skill_names=effective_builtin_skills,
            generation_fingerprint=request_fingerprint,
            reuse_proof_sink=reuse_proof_sink,
            validate_committed_batteries=validate_committed_batteries,
        )
    if npm_skill_metadata:
        return TargetBuildReport(
            language=report.language,
            generated=report.generated,
            skipped=report.skipped,
            refrozen=report.refrozen,
            failed=report.failed,
            advisories=report.advisories,
            metadata={**report.metadata, "npm_skills": npm_skill_metadata},
            exit_code=report.exit_code,
        )
    return report


__all__ = [
    "SyncReport",
    "TypeScriptAnalysis",
    "WorkerFactory",
    "MISSING_INPUT",
    "analyze",
    "atomic_write_manifest",
    "run_build",
    "run_build_in_session",
    "run_sync",
    "validate_overlay",
    "worker_session",
]
