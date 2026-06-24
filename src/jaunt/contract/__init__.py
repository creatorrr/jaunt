"""Contract mode: committed code as source of truth, prose as contract.

This package shares its dotted name with the ``@jaunt.contract`` decorator
exported from :mod:`jaunt`. Importing this subpackage rebinds the
``jaunt.contract`` *attribute* on the parent package to this module object,
which would otherwise shadow the decorator and break ``@jaunt.contract`` usage.

To keep both ``@jaunt.contract`` (decorator) and ``from jaunt.contract import
...`` (subpackage) working, the module object itself is made callable: calling
``jaunt.contract`` (or ``jaunt.contract(...)``) delegates to the real decorator
in :mod:`jaunt.runtime`. Submodule resolution
(``jaunt.contract.runner`` etc.) goes through ``sys.modules`` and is unaffected.
"""

from __future__ import annotations

import sys as _sys
from types import ModuleType as _ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from jaunt.runtime import F


class _ContractModule(_ModuleType):
    """A package module that is also callable as the ``@jaunt.contract`` decorator."""

    def __call__(self, obj: F | None = None, *, deps: object | None = None) -> F | Callable[[F], F]:
        from jaunt.runtime import contract as _contract

        if obj is None:
            return _contract(deps=deps)
        return _contract(obj)


_self = _sys.modules[__name__]
if not isinstance(_self, _ContractModule):
    _self.__class__ = _ContractModule
