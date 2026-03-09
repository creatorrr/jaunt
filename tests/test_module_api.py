from __future__ import annotations

from pathlib import Path

from jaunt.module_api import build_dependency_api_block, build_spec_api_summary, module_api_digest
from jaunt.registry import SpecEntry
from jaunt.spec_ref import normalize_spec_ref


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _entry(
    *,
    module: str,
    qualname: str,
    source_file: str,
    class_name: str | None = None,
    effective_signature: str | None = None,
) -> SpecEntry:
    return SpecEntry(
        kind="magic",
        spec_ref=normalize_spec_ref(f"{module}:{qualname}"),
        module=module,
        qualname=qualname,
        source_file=source_file,
        obj=object(),
        decorator_kwargs={},
        class_name=class_name,
        effective_signature=effective_signature,
    )


def test_build_dependency_api_block_for_top_level_function(tmp_path: Path) -> None:
    src = tmp_path / "mod.py"
    _write(
        src,
        (
            "def play(board: list[str]) -> str:\n"
            '    """Choose the next move.\n'
            "\n"
            "    Raise ValueError for invalid board sizes.\n"
            '    """\n'
            "    ...\n"
        ),
    )
    entry = _entry(module="pkg.mod", qualname="play", source_file=str(src))

    block = build_dependency_api_block(entry)

    assert "kind: function" in block
    assert "signature: def play(board: list[str]) -> str" in block
    assert "doc:" in block
    assert "Choose the next move." in block
    assert "Raise ValueError for invalid board sizes." in block


def test_build_spec_api_summary_for_method_uses_method_metadata(tmp_path: Path) -> None:
    src = tmp_path / "mod.py"
    _write(
        src,
        (
            "class Game:\n"
            "    def winner(self, board: list[str]) -> str | None:\n"
            '        """Return the winner if there is one."""\n'
            "        ...\n"
        ),
    )
    entry = _entry(
        module="pkg.mod",
        qualname="Game.winner",
        source_file=str(src),
        class_name="Game",
    )

    summary = build_spec_api_summary(entry)

    assert summary.kind == "method"
    assert summary.class_name == "Game"
    assert "def winner(self, board: list[str]) -> str | None" in summary.signature
    assert summary.doc == "Return the winner if there is one."


def test_build_spec_api_summary_for_class_includes_members(tmp_path: Path) -> None:
    src = tmp_path / "mod.py"
    _write(
        src,
        (
            "class Game:\n"
            '    """Game engine.\n'
            "\n"
            "    Tracks the full board lifecycle.\n"
            '    """\n'
            "    TURN_LIMIT: int = 9\n"
            "\n"
            "    def winner(self, board: list[str]) -> str | None:\n"
            '        """Return the winner if there is one."""\n'
            "        ...\n"
            "\n"
            "    async def best_move(self, board: list[str]) -> int:\n"
            '        """Choose the strongest move."""\n'
            "        ...\n"
        ),
    )
    entry = _entry(module="pkg.mod", qualname="Game", source_file=str(src))

    summary = build_spec_api_summary(entry)
    block = build_dependency_api_block(entry)

    assert summary.kind == "class"
    assert summary.doc == "Game engine.\n\nTracks the full board lifecycle."
    assert any(member.name == "winner" and member.kind == "method" for member in summary.members)
    assert any(
        member.name == "best_move" and member.kind == "async_method" for member in summary.members
    )
    assert any(
        member.name == "TURN_LIMIT" and member.kind == "class_attribute"
        for member in summary.members
    )
    assert "member:" in block
    assert "name: winner" in block
    assert "signature: def winner(self, board: list[str]) -> str | None" in block
    assert "Tracks the full board lifecycle." in block


def test_module_api_digest_ignores_function_body_only_changes(tmp_path: Path) -> None:
    src = tmp_path / "mod.py"
    _write(
        src,
        (
            "def score(board: list[str]) -> int:\n"
            '    """Count occupied cells."""\n'
            "    return len(board)\n"
        ),
    )
    entry = _entry(module="pkg.mod", qualname="score", source_file=str(src))
    digest_before = module_api_digest([entry])

    _write(
        src,
        (
            "def score(board: list[str]) -> int:\n"
            '    """Count occupied cells."""\n'
            "    total = 0\n"
            "    for cell in board:\n"
            "        total += 1\n"
            "    return total\n"
        ),
    )
    entry_after = _entry(module="pkg.mod", qualname="score", source_file=str(src))

    assert module_api_digest([entry_after]) == digest_before


def test_module_api_digest_changes_when_signature_changes(tmp_path: Path) -> None:
    src = tmp_path / "mod.py"
    _write(src, "def score(board: list[str]) -> int:\n    return len(board)\n")
    entry = _entry(module="pkg.mod", qualname="score", source_file=str(src))
    digest_before = module_api_digest([entry])

    _write(src, "def score(board: tuple[str, ...]) -> int:\n    return len(board)\n")
    entry_after = _entry(module="pkg.mod", qualname="score", source_file=str(src))

    assert module_api_digest([entry_after]) != digest_before


def test_module_api_digest_changes_when_later_doc_lines_change(tmp_path: Path) -> None:
    src = tmp_path / "mod.py"
    _write(
        src,
        (
            "def score(board: list[str]) -> int:\n"
            '    """Count occupied cells.\n'
            "\n"
            "    Raise ValueError when the board is malformed.\n"
            '    """\n'
            "    return len(board)\n"
        ),
    )
    entry = _entry(module="pkg.mod", qualname="score", source_file=str(src))
    digest_before = module_api_digest([entry])

    _write(
        src,
        (
            "def score(board: list[str]) -> int:\n"
            '    """Count occupied cells.\n'
            "\n"
            "    Raise RuntimeError when the board is malformed.\n"
            '    """\n'
            "    return len(board)\n"
        ),
    )
    entry_after = _entry(module="pkg.mod", qualname="score", source_file=str(src))

    assert module_api_digest([entry_after]) != digest_before


def test_module_api_digest_changes_when_class_member_signature_changes(tmp_path: Path) -> None:
    src = tmp_path / "mod.py"
    _write(
        src,
        ("class Game:\n    def winner(self, board: list[str]) -> str | None:\n        ...\n"),
    )
    entry = _entry(module="pkg.mod", qualname="Game", source_file=str(src))
    digest_before = module_api_digest([entry])

    _write(
        src,
        ("class Game:\n    def winner(self, board: tuple[str, ...]) -> str | None:\n        ...\n"),
    )
    entry_after = _entry(module="pkg.mod", qualname="Game", source_file=str(src))

    assert module_api_digest([entry_after]) != digest_before
