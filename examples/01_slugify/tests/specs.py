from __future__ import annotations

import jaunt


@jaunt.test()
def test_slugify_basic_cases() -> None:
    """
    Target: slugify_demo.slugify

    slugify should:
    - strip surrounding whitespace
    - lowercase
    - collapse punctuation/whitespace into a single "-"
    - strip leading/trailing "-"

    Examples:
    - slugify("  Hello, World!  ") == "hello-world"
    - slugify("---Already--Slug---") == "already-slug"
    - slugify("C++ > Java") == "c-java"
    """
    from slugify_demo import slugify

    assert slugify("  Hello, World!  ") == "hello-world"
    assert slugify("---Already--Slug---") == "already-slug"
    assert slugify("C++ > Java") == "c-java"


@jaunt.test()
def test_slugify_rejects_empty() -> None:
    """
    Target: slugify_demo.slugify

    slugify should raise ValueError for:
    - ""
    - "   "
    - strings that become empty after cleaning, like "---"
    """
    import pytest
    from slugify_demo import slugify

    for raw in ["", "   ", "---"]:
        with pytest.raises(ValueError):
            slugify(raw)


@jaunt.test()
def test_post_slug_appends_id_and_validates_post_id() -> None:
    """
    Target: slugify_demo.post_slug

    post_slug should:
    - use slugify(title) for the base
    - append "-<post_id>"
    - raise ValueError if post_id < 1

    Example:
    - post_slug("Hello, World!", post_id=7) == "hello-world-7"
    """
    import pytest
    from slugify_demo import post_slug

    assert post_slug("Hello, World!", post_id=7) == "hello-world-7"
    with pytest.raises(ValueError):
        post_slug("Hello", post_id=0)
