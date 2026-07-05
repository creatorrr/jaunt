# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.header:parse_stub_header
# jaunt:prose-digest=sha256:afa2c054abe44a6bbe0868f62c7bfb4fc1de6193481ca0632cef2838f86ca425
# jaunt:signature=afd0df07e281d7181522edfe04a448750d5d487670c0e20f7af7b08430eb7502
# jaunt:body-digest=sha256:b07288651c4ebd525f561ab8854fa6f812a2e7048d33ddcba2c9d886c0c9c73b
# jaunt:strength=14/21
# jaunt:tool-version=1.5.1
import pytest
from jaunt.header import parse_stub_header
from jaunt.header import STUB_HEADER_MARKER


# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert parse_stub_header("") == None
    assert parse_stub_header(STUB_HEADER_MARKER + "\n# jaunt:kind=stub") == {"kind": "stub"}


# <<< jaunt:derived examples
