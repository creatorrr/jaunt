# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=contract_properties.specs:allocate
# jaunt:prose-digest=sha256:0346297030b0c9a1b2137c6c1f94d1b1b8a8142b9e15aa0d3b42cd3b5346a70f
# jaunt:signature=6b5b7c98ac210e84618bf43f6e675fce577344bb0754f55f3a566350264411ff
# jaunt:body-digest=sha256:8a31590a827001947ade686f8d4d13492847534c46fb21aca1207560a7c50db6
# jaunt:strength=9/9
# jaunt:strength-excluded=2
# jaunt:tool-version=1.5.2
import pytest
from contract_properties.specs import allocate

# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert allocate(100, 3) == [34, 33, 33]
    assert allocate(7, 7) == [1, 1, 1, 1, 1, 1, 1]
# <<< jaunt:derived examples

# >>> jaunt:derived properties
from hypothesis import given, settings
from hypothesis import strategies as st

@given(t=st.from_type(int), n=st.integers(min_value=1, max_value=50))
@settings(max_examples=50, derandomize=True, database=None, deadline=None)
def test_prop_1(t, n):  # derived from: Properties
    assert sum(allocate(t, n)) == t

@given(t=st.from_type(int), n=st.integers(min_value=1, max_value=50))
@settings(max_examples=50, derandomize=True, database=None, deadline=None)
def test_prop_2(t, n):  # derived from: Properties
    assert max(allocate(t, n)) - min(allocate(t, n)) <= 1
# <<< jaunt:derived properties
