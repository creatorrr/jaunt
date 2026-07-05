# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.module_contract:build_module_contract
# jaunt:prose-digest=sha256:f39c9978e55416e8b35dc0b2ae03e5dcd1de6020e30a119692305cfbad7fbe3f
# jaunt:signature=7145b81461da96466d728a025c788d01769d094b7b6e4e2243a3240c502f5883
# jaunt:body-digest=sha256:1b2bf888a6a98cb306d6b8ea7e69bf37a22562da6e922c7d7cf46f1106b705c6
# jaunt:strength=4/53
# jaunt:tool-version=1.5.1
import pytest
from jaunt.module_contract import build_module_contract


# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert build_module_contract(entries=[], expected_names=[]).handwritten_names == ()
    assert build_module_contract(entries=[], expected_names=[]).symbols == ()
    assert build_module_contract(entries=[], expected_names=[]).source_file == ""


# <<< jaunt:derived examples
