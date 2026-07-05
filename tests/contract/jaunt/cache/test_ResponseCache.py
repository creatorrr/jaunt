# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.cache:ResponseCache
# jaunt:prose-digest=sha256:2b040c0fcea7a2f74e196a6d2674cbc87c50ca86790aaf649a7d38c967d31f0c
# jaunt:signature=ee13f35b11a1d6c04277740a9230d32b253978a553b48b962c2be7780874fffa
# jaunt:body-digest=sha256:32fef62133ea5a49574e5bb6751c90e8c3d4ac38344838b704e89cc4cd6ec0be
# jaunt:strength=11/117
# jaunt:tool-version=1.5.1
import pytest
from jaunt.cache import ResponseCache
from jaunt.cache import Path


# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert ResponseCache(Path("/tmp/jaunt-cache-probe"), enabled=True).hits == 0
    assert ResponseCache(Path("/tmp/jaunt-cache-probe"), enabled=True).misses == 0
    assert ResponseCache(Path("/tmp/jaunt-cache-probe"), enabled=False).get("k") == None


# <<< jaunt:derived examples
