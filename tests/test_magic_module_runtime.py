"""Tests for jaunt.magic_module — call-time registration + Approach-A runtime hook."""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

import jaunt
from jaunt import registry
from jaunt.errors import JauntError, JauntNotBuiltError

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

_FIXTURE_MODULES = {
    "gm_mod",
    "twice_mod",
    "bad_name_mod",
    "zero_mod",
    "capture_mod",
    "sig_mod",
    "meta_mod",
    "circ_probe_mod",
}


def _purge() -> None:
    registry.clear_registries()
    for key in list(sys.modules):
        if key == "__generated__" or key.startswith("__generated__."):
            del sys.modules[key]
    for key in _FIXTURE_MODULES:
        sys.modules.pop(key, None)


@pytest.fixture(autouse=True)
def _isolate():
    _purge()
    yield
    _purge()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_governed(tmp_path: Path, name: str = "gm_mod", body: str = GOVERNED) -> None:
    _write(tmp_path / f"{name}.py", body)


def _write_generated(tmp_path: Path, name: str = "gm_mod", body: str = GENERATED) -> None:
    _write(tmp_path / "__generated__" / "__init__.py", "")
    _write(tmp_path / "__generated__" / f"{name}.py", body)


# --- registration ----------------------------------------------------------


def test_import_registers_module_origin_entries(tmp_path, monkeypatch):
    _write_governed(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.import_module("gm_mod")

    entries = {e.qualname: e for e in registry.get_magic_registry().values()}
    assert set(entries) == {"Email", "parse_email"}
    for e in entries.values():
        assert e.origin == "module"
        assert e.obj is None
        assert e.decorator_kwargs == {"prompt": "module prompt"}
    defaults = registry.get_module_magic_defaults("gm_mod")
    assert defaults is not None
    assert defaults.decorator_kwargs == {"prompt": "module prompt"}


# --- built path ------------------------------------------------------------


def test_built_path_external_access_and_sibling_call(tmp_path, monkeypatch):
    _write_governed(tmp_path)
    _write_generated(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    mod = importlib.import_module("gm_mod")

    assert mod.parse_email("x").subject == "x"  # triggers resolution
    assert isinstance(mod.parse_email("x"), mod.Email)
    assert mod.helper("t") == "t"  # sibling call, late binding
    assert mod.Email.__jaunt_spec_ref__ == "gm_mod:Email"
    assert type(mod) is types.ModuleType  # swap-back


# --- not-built path --------------------------------------------------------


def test_not_built_path_raises_actionable(tmp_path, monkeypatch):
    _write_governed(tmp_path)  # no generated dir
    monkeypatch.syspath_prepend(str(tmp_path))
    mod = importlib.import_module("gm_mod")

    with pytest.raises(JauntNotBuiltError, match="jaunt build"):
        mod.parse_email("x")
    with pytest.raises(JauntNotBuiltError, match="jaunt build"):
        mod.Email()
    assert type(mod) is types.ModuleType


# --- errors (spec §5) ------------------------------------------------------


def test_call_outside_module_scope_errors():
    with pytest.raises(JauntError, match="module top level"):
        jaunt.magic_module("gm_mod")


def test_double_call_errors(tmp_path, monkeypatch):
    _write(
        tmp_path / "twice_mod.py",
        "import jaunt\n"
        "jaunt.magic_module(__name__)\n"
        "def f():\n    ...\n"
        "jaunt.magic_module(__name__)\n",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(JauntError, match="already called"):
        importlib.import_module("twice_mod")


def test_unknown_module_name_errors(tmp_path, monkeypatch):
    _write(
        tmp_path / "bad_name_mod.py",
        'import jaunt\njaunt.magic_module("no_such_module_xyz")\n',
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(JauntError, match="sys.modules"):
        importlib.import_module("bad_name_mod")


# --- zero-spec warning -----------------------------------------------------


def test_zero_spec_warns_but_registers_defaults(tmp_path, monkeypatch):
    _write(
        tmp_path / "zero_mod.py",
        "import jaunt\njaunt.magic_module(__name__, prompt='p')\ndef real():\n    return 1\n",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.warns(UserWarning, match="no top-level stubs"):
        importlib.import_module("zero_mod")
    assert registry.get_module_magic_defaults("zero_mod") is not None
    assert registry.get_magic_registry() == {}


# --- capture warning -------------------------------------------------------


def test_capture_warning_surfaces(tmp_path, monkeypatch):
    _write(
        tmp_path / "capture_mod.py",
        "import jaunt\n"
        "jaunt.magic_module(__name__)\n"
        "class Email:\n"
        '    """spec"""\n'
        "def parse(raw: str) -> 'Email':\n"
        "    ...\n"
        "class Signed(Email):\n"
        "    def sign(self):\n        return 1\n"
        "DEFAULT = parse('x')\n",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.warns(UserWarning, match="pre-rebind stub"):
        importlib.import_module("capture_mod")


# --- corrupt generated-class parity (codex review P2) ----------------------


def test_generated_class_wrong_type_raises(tmp_path, monkeypatch):
    _write_governed(tmp_path)
    # __generated__.gm_mod defines Email as a non-type (corrupt artifact).
    _write_generated(
        tmp_path,
        body="Email = 123\n\n\ndef parse_email(raw):\n    return raw\n",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    mod = importlib.import_module("gm_mod")

    with pytest.raises(JauntError, match="is not a class"):
        _ = mod.Email


# --- custom-metaclass parity (codex review P2) -----------------------------


def test_custom_metaclass_rejected_at_finalize(tmp_path, monkeypatch):
    from jaunt.module_magic import finalize_module_magic

    _write(
        tmp_path / "meta_mod.py",
        "import jaunt\n"
        "jaunt.magic_module(__name__)\n"
        "class Meta(type):\n    pass\n"
        "class Model(metaclass=Meta):\n"
        '    """spec"""\n',
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.import_module("meta_mod")

    with pytest.raises(JauntError, match="Custom metaclasses are not supported"):
        finalize_module_magic("meta_mod")


# --- @jaunt.sig top-level regression ---------------------------------------


def test_toplevel_sig_in_governed_module_still_errors(tmp_path, monkeypatch):
    _write(
        tmp_path / "sig_mod.py",
        "import jaunt\n"
        "jaunt.magic_module(__name__)\n"
        "@jaunt.sig\n"
        "def f(self, x: int) -> int:\n    ...\n",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(JauntError, match="whole-class"):
        importlib.import_module("sig_mod")


def test_helper_first_access_resolves_specs_before_helper_runs(tmp_path, monkeypatch):
    _write_governed(tmp_path)
    _write_generated(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    mod = importlib.import_module("gm_mod")
    # The FIRST external access is the handwritten helper, not a spec name.
    # Its global lookup of parse_email bypasses __getattribute__, so resolution
    # must fire on the helper access itself or the raw stub runs silently.
    assert mod.helper("x") == "x"
    assert type(mod) is types.ModuleType  # resolution fired and swapped back


CIRCULAR_GOVERNED = '''
import jaunt

jaunt.magic_module(__name__)


def parse_email(raw: str) -> str:
    """Parse."""
    ...


import circ_probe_mod  # noqa: E402 - trailing circular import (the sharp case)

READY = True
'''

CIRCULAR_PROBE = """
import gm_mod

PROBED = hasattr(gm_mod, "no_such_attr")
HELPER_TYPE = type(gm_mod).__name__
"""


def test_circular_probe_mid_execution_does_not_resolve(tmp_path, monkeypatch):
    _write(tmp_path / "gm_mod.py", CIRCULAR_GOVERNED)
    _write(tmp_path / "circ_probe_mod.py", CIRCULAR_PROBE)
    _write_generated(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("circ_probe_mod", None)

    mod = importlib.import_module("gm_mod")
    # The probe ran while gm_mod was mid-execution (after all stubs were
    # defined, before READY): it must NOT have triggered resolution.
    probe = sys.modules["circ_probe_mod"]
    assert probe.PROBED is False
    assert probe.HELPER_TYPE == "_MagicModule"  # still intercepting at probe time
    assert mod.READY is True  # trailing code ran untouched
    # Post-import, first external access resolves to generated code as usual.
    assert mod.parse_email("x").subject == "x"
    sys.modules.pop("circ_probe_mod", None)
