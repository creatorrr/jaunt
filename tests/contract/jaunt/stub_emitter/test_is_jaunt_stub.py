# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.stub_emitter:is_jaunt_stub
# jaunt:prose-digest=sha256:958e76c8ad56643be605bc6d11f6d1c4aa37aeefb657781599fbffa35b8f65b2
# jaunt:signature=03b3c3d9413df032d88c337ab9f73222620a4797662e4c832a038159ae7b4b1f
# jaunt:body-digest=sha256:64cca9df4e2b66e908e71432a6ecb057a3db326ef6d3cce40828ff183872d418
# jaunt:strength=3/6
# jaunt:tool-version=1.5.1
import pytest
from jaunt.stub_emitter import is_jaunt_stub
from jaunt.stub_emitter import Path


# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert is_jaunt_stub(Path("/nonexistent/does-not-exist.pyi")) == False


# <<< jaunt:derived examples
