# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.digest:load_function_node
# jaunt:prose-digest=sha256:58f5bf9f68a59c81d0392c545388d9cb83658d157712aa0d909e09adb5d652fe
# jaunt:signature=a4dfb0466616739ba04512abc746dfa4252a198e90c7a000b867ce84caae190e
# jaunt:body-digest=sha256:88526e6bacdfd5424125b8dd62823a61efb37e18547b52d9f6d3c0ca07dce488
# jaunt:strength=1/5
# jaunt:tool-version=1.5.1
import pytest
from jaunt.digest import load_function_node


# >>> jaunt:derived errors
def test_raises_valueerror():  # derived from: Raises
    with pytest.raises(ValueError):
        load_function_node("m.py", "A.b")


# <<< jaunt:derived errors
