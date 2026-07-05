# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.parse_cache:ParseCache
# jaunt:prose-digest=sha256:de646fd888f93cb7f02a2a7ac6e571de82ce71bd2909c032d77ba1bd910c0ab7
# jaunt:signature=2a428c4cfc1a5f526b7651b63f729582a06bab210520c6f96c85dbf1732c864e
# jaunt:body-digest=sha256:2bb6e72bc0f6ee3a3b3aecbe978952391e245f3a651d2bff3f9ea6086de921c1
# jaunt:strength=4/62
# jaunt:tool-version=1.5.1
import pytest
from jaunt.parse_cache import ParseCache
from jaunt.parse_cache import Path


# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert (
        ParseCache(Path("/tmp/jaunt-parse-probe")).parse("/nonexistent/does_not_exist.py") == None
    )


# <<< jaunt:derived examples
