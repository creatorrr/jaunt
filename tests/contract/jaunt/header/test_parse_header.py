# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.header:parse_header
# jaunt:prose-digest=sha256:067eda08d85e3dc6333066ad6ed22dcc3e6573b8a30ff82cb84a4ba9db49daf9
# jaunt:signature=afd0df07e281d7181522edfe04a448750d5d487670c0e20f7af7b08430eb7502
# jaunt:body-digest=sha256:4a53517e64c8544e677dcd7dd57bd381171eddff59e383ab84c422d6af0f64fc
# jaunt:strength=14/21
# jaunt:tool-version=1.5.1
import pytest
from jaunt.header import parse_header
from jaunt.header import HEADER_MARKER


# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert parse_header("") == None
    assert parse_header(HEADER_MARKER) == {}
    assert parse_header(HEADER_MARKER + "\n# jaunt:kind=build") == {"kind": "build"}


# <<< jaunt:derived examples
