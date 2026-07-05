from pathlib import Path

from jaunt.generate.codex_backend import ADVISORIES_INSTRUCTION, parse_advisories


def test_block_with_items():
    msg = (
        "Done.\n\nADVISORIES:\n"
        "- spec of parse_email contradicts Email docstring\n"
        "retry logic in deps.http looks buggy\n"
    )
    assert parse_advisories(msg) == (
        "spec of parse_email contradicts Email docstring",
        "retry logic in deps.http looks buggy",
    )


def test_none_sentinel_and_missing_block():
    assert parse_advisories("Done.\nADVISORIES: none\n") == ()
    assert parse_advisories("Done. No block here.") == ()
    assert parse_advisories("") == ()


def test_heading_variants_and_last_block_wins():
    msg = "ADVISORIES:\n- early noise\n\nfinal text\n## ADVISORIES\n- real item\n"
    assert parse_advisories(msg) == ("real item",)


def test_malformed_content_kept_as_raw_text():
    msg = "ADVISORIES:\n   \n:::weird::: but real concern\n"
    assert parse_advisories(msg) == (":::weird::: but real concern",)


def test_instruction_not_in_fingerprinted_templates():
    # Zero-invalidation guard: the instruction must never migrate into the
    # prompt template files, which DO participate in the generation fingerprint.
    for p in Path("src/jaunt/prompts").glob("*.md"):
        assert "ADVISORIES" not in p.read_text(encoding="utf-8"), p
    assert "ADVISORIES:" in ADVISORIES_INSTRUCTION
