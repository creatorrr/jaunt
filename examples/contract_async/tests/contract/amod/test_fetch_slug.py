# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=amod:fetch_slug
# jaunt:prose-digest=sha256:d49febf93e56f265526749485faa7b48d7013e5393bf1313297bccf0e7e0bf0a
# jaunt:signature=fcb0a56ad98814d3deb565caba94c6460fd679fdc83b5a601861ac6f56a9cfb4
# jaunt:body-digest=sha256:79131fceb7ac8e95439621ae6ca3ce778aedad5907eae3f557c3cde01c5d18db
# jaunt:strength=6/8
# jaunt:tool-version=1.0.0rc6
import pytest
from amod import fetch_slug

# >>> jaunt:derived examples
async def test_examples():  # derived from: Examples
    assert await fetch_slug("  Hello, World!  ") == "hello-world"
    assert await fetch_slug("C++ > Java") == "c-java"
# <<< jaunt:derived examples

# >>> jaunt:derived errors
async def test_raises_valueerror():  # derived from: Raises
    with pytest.raises(ValueError):
        await fetch_slug("")
# <<< jaunt:derived errors
