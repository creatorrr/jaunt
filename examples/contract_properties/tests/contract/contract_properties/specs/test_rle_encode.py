# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=contract_properties.specs:rle_encode
# jaunt:prose-digest=sha256:1b0490ef3157d0de05563f091e35f17ef78f79fbab660efbbff42feea4df3509
# jaunt:signature=26c1171ebc1057c59837c0c17dfa5f88faf038e3b5edf8e554777dc0276e4709
# jaunt:body-digest=sha256:34a5a5415a979955e9006ee089e30cbbcdc75aa44f354ed5b2a6ee8a84bd1fda
# jaunt:strength=14/14
# jaunt:strength-excluded=3
# jaunt:tool-version=1.5.2
import pytest
from contract_properties.specs import rle_encode
from contract_properties.specs import rle_decode

# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert rle_encode([1, 1, 2, 2, 2, 1]) == [(1, 2), (2, 3), (1, 1)]
# <<< jaunt:derived examples

# >>> jaunt:derived properties
from hypothesis import given, settings
from hypothesis import strategies as st

@given(xs=st.lists(st.integers()))
@settings(max_examples=50, derandomize=True, database=None, deadline=None)
def test_prop_1(xs):  # derived from: Properties
    assert rle_decode(rle_encode(xs)) == xs

@given(xs=st.lists(st.integers()))
@settings(max_examples=50, derandomize=True, database=None, deadline=None)
def test_prop_2(xs):  # derived from: Properties
    assert all(count > 0 for value, count in rle_encode(xs))

@given(xs=st.lists(st.integers()))
@settings(max_examples=50, derandomize=True, database=None, deadline=None)
def test_prop_3(xs):  # derived from: Properties
    assert all(a[0] != b[0] for a, b in zip(rle_encode(xs), rle_encode(xs)[1:]))
# <<< jaunt:derived properties
