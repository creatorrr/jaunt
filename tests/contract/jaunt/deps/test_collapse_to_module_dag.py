# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.deps:collapse_to_module_dag
# jaunt:prose-digest=sha256:0ec34a63b9a3df02e6ccf8ccd1d7f098b3743cd282e7dfc077343880a444a729
# jaunt:signature=64e5203d74997d671db1ea13add5f9800a52e428e77e373907e235bc111d7cb3
# jaunt:body-digest=sha256:6d823325361c278dcc31ea09b67276d92a05538b883eacb75593023567d1a3e1
# jaunt:strength=15/16
# jaunt:tool-version=1.5.1
import pytest
from jaunt.deps import collapse_to_module_dag


# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert collapse_to_module_dag({}) == {}
    assert collapse_to_module_dag({"m1:f": {"m2:g"}}) == {"m1": {"m2"}, "m2": set()}


# <<< jaunt:derived examples
