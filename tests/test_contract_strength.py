from __future__ import annotations

import ast

from jaunt.contract.derive import ContractBlocks, ExampleRow, extract_blocks_structured
from jaunt.contract.strength import compute_strength, format_strength, iter_mutants

STRONG_SRC = '''
def clamp(n: int) -> int:
    """Clamp n into [0, 10]."""
    if n < 0:
        return 0
    if n > 10:
        return 10
    return n
'''

STRONG_DOC = """
Examples:
- -5 -> 0
- 5 -> 5
- 15 -> 10
- 0 -> 0
- 10 -> 10
"""


def test_iter_mutants_produces_multiple_variants() -> None:
    mutants = list(iter_mutants(STRONG_SRC))
    assert len(mutants) >= 5
    assert all(m != STRONG_SRC for m in mutants)
    # Each mutant is still parseable Python.
    import ast

    for m in mutants:
        ast.parse(m)


def test_strong_contract_kills_most_mutants() -> None:
    blocks = extract_blocks_structured(STRONG_DOC)
    killed, applicable = compute_strength(STRONG_SRC, "clamp", blocks, {})
    assert applicable >= 5
    assert killed / applicable >= 0.6
    assert "/" in format_strength(killed, applicable)


def test_vacuous_contract_scores_low() -> None:
    # No example/raises rows -> nothing pins the body -> all mutants survive.
    killed, applicable = compute_strength(STRONG_SRC, "clamp", ContractBlocks(), {})
    assert killed == 0


def test_single_weak_example_survives_many_mutants() -> None:
    blocks = ContractBlocks(examples=(ExampleRow("5", "5"),))
    killed, applicable = compute_strength(STRONG_SRC, "clamp", blocks, {})
    # Only the n=5 passthrough is pinned; boundary mutants survive.
    assert killed < applicable


def _body_statement_count(src: str) -> int:
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef))
    return len(fn.body)


def test_statement_deletion_mutant_is_produced() -> None:
    base_count = _body_statement_count(STRONG_SRC)
    mutants = list(iter_mutants(STRONG_SRC))
    # At least one mutant drops a top-level body statement (and never empties it).
    deletion_mutants = [m for m in mutants if _body_statement_count(m) == base_count - 1]
    assert deletion_mutants
    for m in deletion_mutants:
        assert _body_statement_count(m) >= 1


def test_comparison_boundary_mutant_is_produced() -> None:
    # Boundary mutation fires on comparison thresholds now that comparators are
    # no longer skipped: 'if n < 0' -> 'if n < 1', 'if n > 10' -> 'if n > 11'.
    mutants = list(iter_mutants(STRONG_SRC))
    assert any("n < 1" in m for m in mutants)
    assert any("n > 11" in m for m in mutants)


def test_format_strength_exact() -> None:
    assert format_strength(2, 5) == "2/5"
