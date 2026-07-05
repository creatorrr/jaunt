# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.change_detection:sidecar_path
# jaunt:prose-digest=sha256:eb14ddd4fa1fdbc4f105fb15bb68d2782c14573b30775c07eba67a86bbdb4517
# jaunt:signature=5f6e879fc2d71c6d410368d9d012283a631f4e7b9b565671c124ad3cd4211c3a
# jaunt:body-digest=sha256:4100de7de78bf075c75107ebb18873fd9c5934edbd95be077144ff8129d072a0
# jaunt:strength=4/4
# jaunt:tool-version=1.5.1
import pytest
from jaunt.change_detection import sidecar_path
from jaunt.change_detection import Path


# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert sidecar_path(Path("gen/mod.py")) == Path("gen/mod.py.contract.json")
    assert sidecar_path(Path("x.py")) == Path("x.py.contract.json")


# <<< jaunt:derived examples
