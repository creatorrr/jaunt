# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.module_api:generated_public_api_digest
# jaunt:prose-digest=sha256:a9d023c334dac9e1511af3cf8ff182e439892da24d81cbd5283841f2014e500d
# jaunt:signature=c934457220408af7568e376a82f0f755fd242d1d09b68eaaf495406b3249f40b
# jaunt:body-digest=sha256:d6fac7f3fa09324e4dff144364b356c0c725d386ba129024667c67f28de00978
# jaunt:strength=1/8
# jaunt:tool-version=1.5.1
import pytest
from jaunt.module_api import generated_public_api_digest


# >>> jaunt:derived errors
def test_raises_valueerror():  # derived from: Raises
    with pytest.raises(ValueError):
        generated_public_api_digest("x = 1", "C")


# <<< jaunt:derived errors
