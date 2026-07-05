# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.module_contract:target_modules_by_name
# jaunt:prose-digest=sha256:8cc8e0e7c9ee950f32563a624efeb0f009321b6a6216d2618468a81a91d35126
# jaunt:signature=4e02fcf03afead24450cd2f8f54840271c48933109b979a8c465e80c4f6ef2f9
# jaunt:body-digest=sha256:d614ebbf013bda7f28738fae6b6e7c993f77d6a0ffd8b702cfbabb93b4f78b78
# jaunt:strength=3/11
# jaunt:tool-version=1.5.1
import pytest
from jaunt.module_contract import target_modules_by_name


# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert target_modules_by_name([]) == {}


# <<< jaunt:derived examples
