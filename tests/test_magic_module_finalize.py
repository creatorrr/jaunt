"""Tests for post-import finalize of module-magic entries (obj backfill + parity)."""

from __future__ import annotations

import importlib
import sys
import textwrap
from pathlib import Path

import pytest

from jaunt import discovery, registry
from jaunt.errors import JauntDiscoveryError
from jaunt.module_magic import finalize_module_magic
from jaunt.spec_ref import normalize_spec_ref

GOVERNED = '''
import jaunt

jaunt.magic_module(__name__, prompt="module prompt")


class Email:
    """Email object."""


def parse_email(raw: str) -> "Email":
    """Parse."""
    ...


def helper(raw: str) -> str:
    return parse_email(raw).subject
'''

GENERATED = """
class Email:
    def __init__(self, subject: str = "s"):
        self.subject = subject


def parse_email(raw: str) -> Email:
    return Email(subject=raw)
"""

BASE_MOD = '''
import jaunt


@jaunt.magic
class Base:
    """Base spec class."""
'''

SIG_GOV_MOD = '''
import jaunt
import base_mod

jaunt.magic_module(__name__)


class Sealed:
    """Sealed class spec."""

    @jaunt.sig
    def method(self, x: int) -> int:
        ...


class Derived(base_mod.Base):
    """Derived spec class."""
'''

IMPORTER_MOD = """
import gm_mod
"""

_FIXTURE_MODULES = {"gm_mod", "base_mod", "sig_gov_mod", "importer_mod", "meta_gov_mod"}


def _purge() -> None:
    registry.clear_registries()
    for key in list(sys.modules):
        if key == "__generated__" or key.startswith("__generated__."):
            del sys.modules[key]
    for key in _FIXTURE_MODULES:
        sys.modules.pop(key, None)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_governed(tmp_path: Path) -> None:
    _write(tmp_path / "gm_mod.py", GOVERNED)


def _write_generated(tmp_path: Path) -> None:
    _write(tmp_path / "__generated__" / "__init__.py", "")
    _write(tmp_path / "__generated__" / "gm_mod.py", GENERATED)


def test_finalize_backfills_obj_and_analysis(tmp_path, monkeypatch):
    _purge()
    _write_governed(tmp_path)  # no generated dir
    monkeypatch.syspath_prepend(str(tmp_path))
    discovery.import_and_collect(["gm_mod"], kind="magic")

    entries = {e.qualname: e for e in registry.get_magic_registry().values()}
    fn = entries["parse_email"]
    assert callable(fn.obj) and fn.obj is not None
    assert fn.effective_signature is not None  # same rendering path as decorators
    assert fn.origin == "module"
    cls = entries["Email"]
    assert isinstance(cls.obj, type)  # passes builder's isinstance gates
    assert cls.origin == "module"
    _purge()


def test_finalize_uses_prerebind_snapshot_when_access_already_fired(tmp_path, monkeypatch):
    _purge()
    _write_governed(tmp_path)
    _write_generated(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    mod = importlib.import_module("gm_mod")
    _ = mod.parse_email("x")  # fires resolution, rebinds
    finalize_module_magic("gm_mod")
    entry = next(e for e in registry.get_magic_registry().values() if e.qualname == "parse_email")
    assert entry.obj is mod.__dict__["__jaunt_original_stubs__"]["parse_email"]
    _purge()


def test_finalize_absorbs_sig_methods_and_base_deps(tmp_path, monkeypatch):
    _purge()
    _write(tmp_path / "base_mod.py", BASE_MOD)
    _write(tmp_path / "sig_gov_mod.py", SIG_GOV_MOD)
    monkeypatch.syspath_prepend(str(tmp_path))
    discovery.import_and_collect(["sig_gov_mod"], kind="magic")

    entries = {e.qualname: e for e in registry.get_magic_registry().values()}
    sealed = entries["Sealed"]
    assert sealed.sealed_members == ("method",)
    derived = entries["Derived"]
    assert derived.base_deps == (normalize_spec_ref("base_mod:Base"),)
    _purge()


def test_transitively_imported_governed_module_is_finalized(tmp_path, monkeypatch):
    _purge()
    _write_governed(tmp_path)
    _write(tmp_path / "importer_mod.py", IMPORTER_MOD)
    monkeypatch.syspath_prepend(str(tmp_path))
    discovery.import_and_collect(["importer_mod"], kind="magic")

    entry = next(e for e in registry.get_magic_registry().values() if e.qualname == "parse_email")
    assert entry.obj is not None and callable(entry.obj)
    _purge()


def test_finalize_is_noop_for_ungoverned_or_unimported_module():
    _purge()
    # Ungoverned / never registered — must not raise.
    finalize_module_magic("no_such_module_xyz")
    assert registry.get_magic_registry() == {}
    _purge()


def test_finalize_error_is_wrapped_as_discovery_error(tmp_path, monkeypatch):
    _purge()
    _write(
        tmp_path / "meta_gov_mod.py",
        textwrap.dedent('''
            import jaunt

            jaunt.magic_module(__name__)


            class Meta(type):
                def __new__(mcs, name, bases, ns):
                    return super().__new__(mcs, name, bases, ns)


            class Model(metaclass=Meta):
                """Spec with a custom metaclass."""
        '''),
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(JauntDiscoveryError, match="finalize magic module 'meta_gov_mod'"):
        discovery.import_and_collect(["meta_gov_mod"], kind="magic")
    _purge()
