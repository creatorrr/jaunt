# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.module_api:build_generated_class_api_summary
# jaunt:prose-digest=sha256:cb42c1b65350474d3c68015a30c86388edbfa6101ea4ea033bd798fbaa59326c
# jaunt:signature=b4edf1dced7107b71d73dda15177b5486cb20c55f61ede95ddbfe044e216e573
# jaunt:body-digest=sha256:54694d027b3d48f1d051e6aac5dae067e4ba7fcb7670cc5ec37905cc219d72ef
# jaunt:strength=3/15
# jaunt:tool-version=1.5.1
import pytest
from jaunt.module_api import build_generated_class_api_summary


# >>> jaunt:derived errors
def test_raises_valueerror():  # derived from: Raises
    with pytest.raises(ValueError):
        build_generated_class_api_summary("x = 1", "C", spec_docstring="")


# <<< jaunt:derived errors
