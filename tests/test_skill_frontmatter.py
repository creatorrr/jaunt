from jaunt.skill_agent import strip_leading_frontmatter, validate_skill_markdown
from jaunt.skills_auto import _format_generated_skill_file, parse_generated_skill_meta

BODY = (
    "## What it is\nx\n## Core concepts\nx\n## Common patterns\nx\n"
    "## Gotchas\nx\n## Testing notes\nx\n"
)


def test_strip_removes_single_leading_frontmatter():
    text = "---\nname: foo\ndescription: bar\n---\n" + BODY
    assert strip_leading_frontmatter(text) == BODY


def test_strip_is_noop_without_frontmatter():
    assert strip_leading_frontmatter(BODY) == BODY


def test_strip_is_idempotent():
    text = "---\nname: foo\n---\n" + BODY
    once = strip_leading_frontmatter(text)
    assert strip_leading_frontmatter(once) == once


def test_strip_does_not_touch_mid_document_rules():
    text = BODY + "\n---\n\nmore prose\n"
    assert strip_leading_frontmatter(text) == text


def test_format_generated_skill_strips_model_frontmatter():
    poisoned = "---\nname: spacy\ndescription: model wrote this\n---\n" + BODY
    out = _format_generated_skill_file(dist="spacy", version="3.8.0", body_md=poisoned)
    assert out.count("\n---\n") + out.startswith("---\n") == 2  # exactly one open + one close
    assert "model wrote this" not in out
    assert parse_generated_skill_meta(out) == ("spacy", "3.8.0")


def test_validate_flags_frontmatter_in_model_output():
    poisoned = "---\nname: spacy\n---\n" + BODY
    errs = validate_skill_markdown(poisoned)
    assert any("frontmatter" in e.lower() for e in errs)
