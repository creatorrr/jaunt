"""
Markdown → HTML Renderer — Test Specs
"""

from __future__ import annotations

import jaunt


@jaunt.test()
def test_headings() -> None:
    """
    - "# Hello" → "<h1>Hello</h1>"
    - "## Sub" → "<h2>Sub</h2>"
    - "###### Deep" → "<h6>Deep</h6>"
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test()
def test_inline_formatting() -> None:
    """
    - "**bold**" inside a paragraph → <p><strong>bold</strong></p>
    - "*italic*" → <p><em>italic</em></p>
    - "`code`" → <p><code>code</code></p>
    - "[click](https://x.com)" → <p><a href="https://x.com">click</a></p>
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test()
def test_fenced_code_block() -> None:
    """
    Input:
        ```python
        x = 1 + 2
        ```

    Should produce:
        <pre><code class="language-python">x = 1 + 2</code></pre>

    - Inline formatting must NOT be applied inside code blocks.
    - HTML chars like <, >, & inside the block must be escaped.
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test()
def test_unordered_list() -> None:
    """
    Input:
        - alpha
        - beta
        - gamma

    Should produce a single <ul> with three <li> elements.
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test()
def test_html_escaping() -> None:
    """
    Input: "Use <div> & \"quotes\""

    The <, >, and & must be escaped in the output paragraph.
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test()
def test_empty_input() -> None:
    """
    md_to_html("") should return "".
    md_to_html_fragment("") should return "<div />".
    """
    raise AssertionError("spec stub (generated at test time)")
