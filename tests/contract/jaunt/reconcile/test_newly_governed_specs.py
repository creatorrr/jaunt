# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.reconcile:newly_governed_specs
# jaunt:prose-digest=sha256:1e7452373b59c0878313ebeac0ee985c2ec376ac63686f67c7ad2825a81eed72
# jaunt:signature=9dea93693321cf09ac09d0c58855af301c83fd113ab6c841080e366a1137c606
# jaunt:body-digest=sha256:a69a0a59c0c809b36c33610aff12c4cf0a85a29366f3ffbec0bea97b0c1d9083
# jaunt:strength=3/14
# jaunt:tool-version=1.5.1
import pytest
from jaunt.reconcile import newly_governed_specs


# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert newly_governed_specs([], package_dir=None, generated_dir="__generated__") == {}


# <<< jaunt:derived examples
