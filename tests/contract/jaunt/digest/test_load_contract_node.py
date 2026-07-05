# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.digest:load_contract_node
# jaunt:prose-digest=sha256:8d58c346829d24272e7797a965c88f2e3163aadf9914a04249df652ed98ab4b9
# jaunt:signature=f387e170448dc1b47ba5b1a1b21984d3c4df0ce2a5cfec9344af4ca9e3151f2b
# jaunt:body-digest=sha256:f079d00f38c474199fd4485d75cab3241fd6e3c039532dd23965f2e59227823d
# jaunt:strength=1/15
# jaunt:tool-version=1.5.1
import pytest
from jaunt.digest import load_contract_node


# >>> jaunt:derived errors
def test_raises_valueerror():  # derived from: Raises
    with pytest.raises(ValueError):
        load_contract_node("m.py", "A.b")


# <<< jaunt:derived errors
