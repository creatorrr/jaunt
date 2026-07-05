# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.validation:validate_generated_source
# jaunt:prose-digest=sha256:b10fce831978da9e66fa5255fe4972384e310cf378e5587587142262a2819941
# jaunt:signature=36dd3e5fa0e691dc6c312ba030c7d8f744aedd7cb0161b8dd3ef96a37e3ea122
# jaunt:body-digest=sha256:af62595be8e287eb6e90820466f1240af9cf0e16b49afdc66b4a25bbed3710e3
# jaunt:strength=13/20
# jaunt:tool-version=1.5.1
import pytest
from jaunt.validation import validate_generated_source


# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert validate_generated_source("x = 1", ["x"]) == []
    assert validate_generated_source("def foo(): return 1", ["foo"]) == []
    assert validate_generated_source("y = 2", ["foo"]) == ["Missing top-level definition: foo"]
    assert validate_generated_source("x = 1", []) == []


# <<< jaunt:derived examples
