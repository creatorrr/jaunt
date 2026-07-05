# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.change_detection:read_contract_sidecar
# jaunt:prose-digest=sha256:03b8f1b85f872e6189d6606ece5614dd6a14c84bd685e95fa06137f5c038627c
# jaunt:signature=53b86fdd8fc916bd1cc23af2fce731bd9a32afd9050daf4c66675484f273c5de
# jaunt:body-digest=sha256:da7121e2aae4c8963a711f823eb7fb00ffd2b303fffcb922bff33726940f66a2
# jaunt:strength=2/7
# jaunt:tool-version=1.5.1
import pytest
from jaunt.change_detection import read_contract_sidecar
from jaunt.change_detection import Path


# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert read_contract_sidecar(Path("/nonexistent/does-not-exist.json")) == {}


# <<< jaunt:derived examples
