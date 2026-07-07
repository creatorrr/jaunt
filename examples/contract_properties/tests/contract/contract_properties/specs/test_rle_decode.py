# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=contract_properties.specs:rle_decode
# jaunt:prose-digest=sha256:9ba50e7f56533582d0838f5c0e5e348a2c9050bd3a2852f90c3225cb4fe84028
# jaunt:signature=fc8e332a2d9d33f6908443528cf49b97a7362c87f61116855bd43f4e85bc0aea
# jaunt:body-digest=sha256:e538bf328cfa46134ec3449d69cd729181e95fbf2a92c9ec1541e4a2999f2ef9
# jaunt:strength=2/2
# jaunt:strength-excluded=1
# jaunt:tool-version=1.5.2
import pytest
from contract_properties.specs import rle_decode
from contract_properties.specs import rle_encode

# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert rle_decode([(1, 2), (2, 3)]) == [1, 1, 2, 2, 2]
# <<< jaunt:derived examples

# >>> jaunt:derived properties
from hypothesis import given, settings
from hypothesis import strategies as st

@given(xs=st.lists(st.integers()))
@settings(max_examples=50, derandomize=True, database=None, deadline=None)
def test_prop_1(xs):  # derived from: Properties
    assert rle_decode(rle_encode(xs)) == xs
# <<< jaunt:derived properties
