# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.header:extract_digest_scheme
# jaunt:prose-digest=sha256:9f70467be7e2dbf078edea8ea82dd34b8dedb2cd7acf89bc177588c1f5393664
# jaunt:signature=95321f0b7b8d2ee42d9de429b61ca7465fc5fb5ccc16f16ae72c93fc20e2a36f
# jaunt:body-digest=sha256:4a00cf4792b9e84d8b7251464264eef60b9daf5b0f650308358cfbbaa84a2afe
# jaunt:strength=6/8
# jaunt:tool-version=1.5.1
import pytest
from jaunt.header import extract_digest_scheme
from jaunt.header import HEADER_MARKER


# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert extract_digest_scheme("") == None
    assert extract_digest_scheme(HEADER_MARKER + "\n# jaunt:digest_scheme=2") == 2
    assert extract_digest_scheme(HEADER_MARKER + "\n# jaunt:digest_scheme=x") == None


# <<< jaunt:derived examples
