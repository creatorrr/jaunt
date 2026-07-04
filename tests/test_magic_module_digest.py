"""Digest parity & neutrality battery for magic_module vs decorator mode.

Spec §2 "digest neutrality, stated precisely" / §6 digest strategy: converting a
decorated spec to module style is digest-neutral iff the stub body form is
unchanged, since the jaunt decorator line never enters the digest and the
finalize step renders the signature identically. Because ``spec_ref`` (module +
qualname) is itself part of every digest payload, each parity assertion reuses
the SAME module name across two sequential loads (decorator-style then
module-style, in separate subdirs) rather than two siblings with distinct names.
"""

from __future__ import annotations

import importlib
import sys
import textwrap

from jaunt import discovery, registry
from jaunt.digest import local_digest, structural_digest

_FIXTURE_NAMES = {"conv_mod", "cls_conv", "re_mod", "prompt_mod", "ov_mod", "gov_dec"}


def _purge() -> None:
    registry.clear_registries()
    for key in list(sys.modules):
        if key == "__generated__" or key.startswith("__generated__."):
            del sys.modules[key]
    for name in _FIXTURE_NAMES:
        sys.modules.pop(name, None)


def _load(tmp_path, subdir, module_name, source, monkeypatch, *, extra=None):
    """Write ``<module_name>.py`` into a fresh subdir, import + finalize, return entries.

    Returns ``{qualname: SpecEntry}``. Files are left on disk so digests (which
    read ``entry.source_file``) stay computable after later loads clear the
    registry. Reusing ``module_name`` across loads keeps ``spec_ref`` — and thus
    the digest's ``ref`` field — identical.
    """
    d = tmp_path / subdir
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{module_name}.py").write_text(textwrap.dedent(source), encoding="utf-8")
    for fname, fsrc in (extra or {}).items():
        (d / fname).write_text(textwrap.dedent(fsrc), encoding="utf-8")
    _purge()
    monkeypatch.syspath_prepend(str(d))
    importlib.invalidate_caches()
    discovery.import_and_collect([module_name], kind="magic")
    return {e.qualname: e for e in registry.get_magic_registry().values()}


DEC_FN = '''
import jaunt


@jaunt.magic()
def f(x: int) -> str:
    """D."""
    ...
'''

MOD_FN = '''
import jaunt

jaunt.magic_module(__name__)


def f(x: int) -> str:
    """D."""
    ...
'''


def test_conversion_with_unchanged_ellipsis_body_is_digest_neutral(tmp_path, monkeypatch):
    dec = _load(tmp_path, "a", "conv_mod", DEC_FN, monkeypatch)["f"]
    mod = _load(tmp_path, "b", "conv_mod", MOD_FN, monkeypatch)["f"]
    assert local_digest(dec) == local_digest(mod)  # includes prose
    assert structural_digest(dec) == structural_digest(mod)
    assert dec.effective_signature == mod.effective_signature  # P1: same rendering path
    _purge()


DEC_CLS = '''
import jaunt


@jaunt.magic()
class Email:
    """Email object."""
'''

MOD_CLS = '''
import jaunt

jaunt.magic_module(__name__)


class Email:
    """Email object."""
'''


def test_conversion_neutral_for_docstring_only_class(tmp_path, monkeypatch):
    dec = _load(tmp_path, "a", "cls_conv", DEC_CLS, monkeypatch)["Email"]
    mod = _load(tmp_path, "b", "cls_conv", MOD_CLS, monkeypatch)["Email"]
    assert local_digest(dec) == local_digest(mod)
    assert structural_digest(dec) == structural_digest(mod)
    _purge()


DEC_RE = '''
import jaunt


@jaunt.magic()
def f() -> None:
    """D."""
    raise RuntimeError("spec stub")
'''

MOD_ELLIPSIS = '''
import jaunt

jaunt.magic_module(__name__)


def f() -> None:
    """D."""
    ...
'''


def test_runtime_error_body_conversion_restales(tmp_path, monkeypatch):
    dec = _load(tmp_path, "a", "re_mod", DEC_RE, monkeypatch)["f"]
    mod = _load(tmp_path, "b", "re_mod", MOD_ELLIPSIS, monkeypatch)["f"]
    # RuntimeError body is not a recognized stub form -> its body text enters the
    # structural digest; rewriting to `...` during conversion restales once.
    assert structural_digest(dec) != structural_digest(mod)
    _purge()


def _prompt_mod(prompt: str) -> str:
    return f'''
import jaunt

jaunt.magic_module(__name__, prompt="{prompt}")


def a(x: int) -> int:
    """A."""
    ...


def b(y: str) -> str:
    """B."""
    ...
'''


def test_module_prompt_edit_restales_every_governed_spec(tmp_path, monkeypatch):
    v1 = _load(tmp_path, "a", "prompt_mod", _prompt_mod("v1"), monkeypatch)
    v2 = _load(tmp_path, "b", "prompt_mod", _prompt_mod("v2"), monkeypatch)
    assert structural_digest(v1["a"]) != structural_digest(v2["a"])
    assert structural_digest(v1["b"]) != structural_digest(v2["b"])
    _purge()


def _ov_mod(override: str) -> str:
    return f'''
import jaunt

jaunt.magic_module(__name__, prompt="modp")


def a(x: int) -> int:
    """A."""
    ...


@jaunt.magic(prompt="{override}")
def b(y: str) -> str:
    """B."""
    ...
'''


def test_per_symbol_override_wins_in_digest(tmp_path, monkeypatch):
    e1 = _load(tmp_path, "a", "ov_mod", _ov_mod("mine1"), monkeypatch)
    a1 = structural_digest(e1["a"])
    b1 = structural_digest(e1["b"])
    e2 = _load(tmp_path, "b", "ov_mod", _ov_mod("mine2"), monkeypatch)
    a2 = structural_digest(e2["a"])
    b2 = structural_digest(e2["b"])
    # sibling `a` inherits the module default only -> unaffected by `b`'s override change
    assert a1 == a2
    # per-symbol override reaches `b`'s digest
    assert b1 != b2
    # and the override actually won the key-by-key merge
    assert e2["a"].decorator_kwargs["prompt"] == "modp"
    assert e2["b"].decorator_kwargs["prompt"] == "mine2"
    _purge()


GOV_DEC = '''
import jaunt

jaunt.magic_module(__name__, prompt="modp")


@jaunt.magic()
def d(x: int) -> int:
    """D."""
    ...
'''

UNGOV_DEC = '''
import jaunt


@jaunt.magic()
def d(x: int) -> int:
    """D."""
    ...
'''


def test_module_kwargs_reach_decorated_symbols_digest(tmp_path, monkeypatch):
    gov = _load(tmp_path, "a", "gov_dec", GOV_DEC, monkeypatch)["d"]
    ungov = _load(tmp_path, "b", "gov_dec", UNGOV_DEC, monkeypatch)["d"]
    assert gov.decorator_kwargs.get("prompt") == "modp"
    assert "prompt" not in ungov.decorator_kwargs
    # merged module default feeds _stable_decorator_kwargs -> distinct structural digest
    assert structural_digest(gov) != structural_digest(ungov)
    _purge()
