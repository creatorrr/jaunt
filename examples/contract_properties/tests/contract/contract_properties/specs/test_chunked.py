# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=contract_properties.specs:chunked
# jaunt:prose-digest=sha256:ea215817bdffe069ccab7851ce0f1e4062b847f970912be8beeaa5e6962ffd06
# jaunt:signature=11cd933aa57592b40f7eaf4f6839b206fa174a77685e294e790f16c920a38f77
# jaunt:body-digest=sha256:ce3d5afba0bda7a93bae659fc4e22051952f07fb5fbd9b5f338504e8f6b59850
# jaunt:strength=4/4
# jaunt:strength-excluded=2
# jaunt:tool-version=1.5.2
import pytest
from contract_properties.specs import chunked

# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert chunked([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
# <<< jaunt:derived examples

# >>> jaunt:derived properties
from hypothesis import given, settings
from hypothesis import strategies as st

@given(xs=st.lists(st.integers()), n=st.integers(min_value=1, max_value=10))
@settings(max_examples=50, derandomize=True, database=None, deadline=None)
def test_prop_1(xs, n):  # derived from: Properties
    assert sum(chunked(xs, n), []) == xs

@given(xs=st.lists(st.integers()), n=st.integers(min_value=1, max_value=10))
@settings(max_examples=50, derandomize=True, database=None, deadline=None)
def test_prop_2(xs, n):  # derived from: Properties
    assert all(len(c) <= n for c in chunked(xs, n))
# <<< jaunt:derived properties
