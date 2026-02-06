"""
Markdown → HTML Renderer — Jaunt Example

A subset Markdown-to-HTML converter. No external dependencies.
"""

from __future__ import annotations

import jaunt


@jaunt.magic()
def md_to_html(source: str) -> str:
    """
    Convert a subset of Markdown to HTML.

    Supported syntax (in order of precedence):

    Block-level (separated by blank lines):
    - Headings: "# H1" through "###### H6" → <h1>…</h1> through <h6>…</h6>
    - Fenced code blocks: ```lang\\n...\\n``` → <pre><code class="language-{lang}">…</code></pre>
      (HTML-escape contents; lang is optional)
    - Unordered lists: lines starting with "- " → <ul><li>…</li></ul>
    - Blockquotes: lines starting with "> " → <blockquote><p>…</p></blockquote>
    - Paragraphs: anything else → <p>…</p>

    Inline (applied within block content, NOT inside code blocks):
    - Bold: **text** → <strong>text</strong>
    - Italic: *text* → <em>text</em>  (single asterisk, not inside **)
    - Inline code: `text` → <code>text</code>  (no nested formatting)
    - Links: [label](url) → <a href="url">label</a>

    Edge cases:
    - Empty input → empty string.
    - HTML special characters (&, <, >) in text must be escaped
      (except in the generated HTML tags themselves).
    - Nested bold/italic like ***bold italic*** → <strong><em>…</em></strong>
    - Inline formatting is NOT applied inside code blocks.
    """
    raise RuntimeError("spec stub (generated at build time)")


@jaunt.magic(deps=[md_to_html])
def md_to_html_fragment(source: str, *, wrapper_tag: str = "div") -> str:
    """
    Render markdown and wrap the result in a single HTML element.

    - Call md_to_html(source) for the inner content.
    - Wrap in <{wrapper_tag}>…</{wrapper_tag}>.
    - If source is empty, return an empty self-closing tag: <{wrapper_tag} />.
    - Raise ValueError if wrapper_tag contains whitespace or '<' or '>'.
    """
    raise RuntimeError("spec stub (generated at build time)")
