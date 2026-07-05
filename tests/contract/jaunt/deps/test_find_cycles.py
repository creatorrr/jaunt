# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.deps:find_cycles
# jaunt:prose-digest=sha256:5a223829a71a178f5374616b5a63583307792b31c59b463bdce4337941ab1d5d
# jaunt:signature=a0ae06ac5b3ad88b80b836a8bbf793689b2e84f9628392035629b7a32d0d5756
# jaunt:body-digest=sha256:a3372df0295c1f55510fa35593f553939cc57636096fd9c9a63e1636a54a269d
# jaunt:strength=22/28
# jaunt:tool-version=1.5.1
import pytest
from jaunt.deps import find_cycles


# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert find_cycles({}) == []
    assert find_cycles({"a": {"a"}}) == [["a"]]
    assert find_cycles({"a": {"b"}, "b": {"a"}}) == [["a", "b"]]
    assert find_cycles({"a": {"b"}, "b": set()}) == []


# <<< jaunt:derived examples
