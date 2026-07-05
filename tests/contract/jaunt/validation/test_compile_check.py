# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.validation:compile_check
# jaunt:prose-digest=sha256:498b17911b0c57e0d04d187bbccbb8be3408f390b4d626654588d041027b4dfb
# jaunt:signature=06bfee48248a2eb27ce97d0f734a563a218286b5690f0765ec557a26a092afa5
# jaunt:body-digest=sha256:78a5b69d2bb31d8cfd4c93830960c48b16e9ec578d430a6012c9755bede2b6d5
# jaunt:strength=3/8
# jaunt:tool-version=1.5.1
import pytest
from jaunt.validation import compile_check


# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert compile_check("x = 1", "f.py") == []
    assert compile_check("", "f.py") == []
    assert compile_check("def foo(): return 1", "f.py") == []


# <<< jaunt:derived examples
