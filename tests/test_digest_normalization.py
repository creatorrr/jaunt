from __future__ import annotations

from pathlib import Path
from typing import Literal

from jaunt.digest import local_digest
from jaunt.registry import SpecEntry
from jaunt.spec_ref import normalize_spec_ref


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _entry(
    *,
    kind: Literal["magic", "test", "contract"],
    spec_ref: str,
    module: str,
    qualname: str,
    source_file: str,
    decorator_kwargs: dict[str, object] | None = None,
) -> SpecEntry:
    return SpecEntry(
        kind=kind,
        spec_ref=normalize_spec_ref(spec_ref),
        module=module,
        qualname=qualname,
        source_file=source_file,
        obj=object(),
        decorator_kwargs=decorator_kwargs or {},
    )


def test_ruff_reformat_is_invariant(tmp_path: Path) -> None:
    p = tmp_path / "m.py"
    _write(
        p,
        """
def process(name: str, count: int = 1) -> dict[str, object]:
    \"\"\"Process a named batch.\"\"\"
    label = 'ready'
    payload = {'name': name, 'count': count, 'label': label}
    return payload
""".lstrip(),
    )
    entry = _entry(
        kind="magic",
        spec_ref="m:process",
        module="m",
        qualname="process",
        source_file=str(p),
    )
    d1 = local_digest(entry)

    _write(
        p,
        """
def process(
    name: str,
    count: int = 1,
) -> dict[str, object]:
    \"\"\"Process a named batch.\"\"\"
    label = "ready"

    payload = {"name": name, "count": count, "label": label}
    return payload
""".lstrip(),
    )
    d2 = local_digest(entry)

    assert d1 == d2


def test_comment_only_edit_is_invariant(tmp_path: Path) -> None:
    p = tmp_path / "m.py"
    _write(
        p,
        """
def process(name: str) -> str:
    \"\"\"
    Process a name.

    Return the normalized display name.
    \"\"\"
    value = name.strip()
    return value.title()
""".lstrip(),
    )
    entry = _entry(
        kind="magic",
        spec_ref="m:process",
        module="m",
        qualname="process",
        source_file=str(p),
    )
    d1 = local_digest(entry)

    _write(
        p,
        """
# leading module comment
def process(name: str) -> str:  # signature comment
    \"\"\"
        Process a name.

        Return the normalized display name.
    \"\"\"
    # normalize first
    value = name.strip()
    return value.title()  # format for display
""".lstrip(),
    )
    d2 = local_digest(entry)

    assert d1 == d2


def test_async_sync_flip_changes_digest(tmp_path: Path) -> None:
    p = tmp_path / "m.py"
    _write(
        p,
        """
def fetch(url: str) -> str:
    \"\"\"Fetch a URL.\"\"\"
    return url
""".lstrip(),
    )
    entry = _entry(
        kind="magic",
        spec_ref="m:fetch",
        module="m",
        qualname="fetch",
        source_file=str(p),
    )
    d1 = local_digest(entry)

    _write(
        p,
        """
async def fetch(url: str) -> str:
    \"\"\"Fetch a URL.\"\"\"
    return url
""".lstrip(),
    )
    d2 = local_digest(entry)

    assert d1 != d2


def test_default_value_change_changes_digest(tmp_path: Path) -> None:
    p = tmp_path / "m.py"
    _write(
        p,
        """
def f(x: int = 1) -> int:
    return x
""".lstrip(),
    )
    entry = _entry(kind="magic", spec_ref="m:f", module="m", qualname="f", source_file=str(p))
    d1 = local_digest(entry)

    _write(
        p,
        """
def f(x: int = 2) -> int:
    return x
""".lstrip(),
    )
    d2 = local_digest(entry)

    assert d1 != d2


def test_rename_changes_digest(tmp_path: Path) -> None:
    alpha_path = tmp_path / "alpha.py"
    beta_path = tmp_path / "beta.py"
    _write(
        alpha_path,
        """
def alpha(value: int) -> int:
    \"\"\"Return the value unchanged.\"\"\"
    return value
""".lstrip(),
    )
    _write(
        beta_path,
        """
def beta(value: int) -> int:
    \"\"\"Return the value unchanged.\"\"\"
    return value
""".lstrip(),
    )
    alpha_entry = _entry(
        kind="magic",
        spec_ref="m:alpha",
        module="m",
        qualname="alpha",
        source_file=str(alpha_path),
    )
    beta_entry = _entry(
        kind="magic",
        spec_ref="m:beta",
        module="m",
        qualname="beta",
        source_file=str(beta_path),
    )

    assert local_digest(alpha_entry) != local_digest(beta_entry)


def test_class_attribute_value_change_changes_digest(tmp_path: Path) -> None:
    p = tmp_path / "m.py"
    _write(
        p,
        """
class Worker:
    \"\"\"Run background work.\"\"\"

    retries: int = 3

    def run(self) -> None:
        ...
""".lstrip(),
    )
    entry = _entry(
        kind="magic",
        spec_ref="m:Worker",
        module="m",
        qualname="Worker",
        source_file=str(p),
    )
    d1 = local_digest(entry)

    _write(
        p,
        """
class Worker:
    \"\"\"Run background work.\"\"\"

    retries: int = 5

    def run(self) -> None:
        ...
""".lstrip(),
    )
    d2 = local_digest(entry)

    assert d1 != d2


def test_preserve_body_edit_changes_digest(tmp_path: Path) -> None:
    p = tmp_path / "m.py"
    _write(
        p,
        """
import jaunt


class Worker:
    \"\"\"Run background work.\"\"\"

    def run(self) -> None:
        ...

    @jaunt.preserve
    def priority(self) -> int:
        return 1
""".lstrip(),
    )
    entry = _entry(
        kind="magic",
        spec_ref="m:Worker",
        module="m",
        qualname="Worker",
        source_file=str(p),
    )
    d1 = local_digest(entry)

    _write(
        p,
        """
import jaunt


class Worker:
    \"\"\"Run background work.\"\"\"

    def run(self) -> None:
        ...

    @jaunt.preserve
    def priority(self) -> int:
        return 2
""".lstrip(),
    )
    d2 = local_digest(entry)

    assert d1 != d2
