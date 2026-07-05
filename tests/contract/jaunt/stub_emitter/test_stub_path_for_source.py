# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.stub_emitter:stub_path_for_source
# jaunt:prose-digest=sha256:60f7f705fa85c814dd0fef89a25f9b53b8b911fd5cf18c3008eb2b31c793bb22
# jaunt:signature=024adc23cb0bf32e725cefe38f50a68ec4bb45bfd3887a7618da2a3ecde0836f
# jaunt:body-digest=sha256:d0d769ccdbc33de8fe8275e3e86d144ea754009ad31446b97bdab564998d5f72
# jaunt:strength=3/3
# jaunt:tool-version=1.5.1
import pytest
from jaunt.stub_emitter import stub_path_for_source
from jaunt.stub_emitter import Path


# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert stub_path_for_source("pkg/mod.py") == Path("pkg/mod.pyi")
    assert stub_path_for_source(Path("a/b.py")) == Path("a/b.pyi")


# <<< jaunt:derived examples
