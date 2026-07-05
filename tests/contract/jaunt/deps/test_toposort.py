# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.deps:toposort
# jaunt:prose-digest=sha256:c05c0f8898ae5fd9424c873dd26ce9c503830a34dd5a2299e4570de1280b8090
# jaunt:signature=aa445a38e99e8b9890129a8437e9f3d683581f7d0cb8cbd2a4c9e2010eb749a3
# jaunt:body-digest=sha256:b0ab6f5027f7e5e1eb5d4e2a8c95d5ffa39a6cd0c8dad07e462113940556ad8f
# jaunt:strength=21/28
# jaunt:tool-version=1.5.1
import pytest
from jaunt.deps import toposort
from jaunt.deps import JauntDependencyCycleError


# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert toposort({}) == []
    assert toposort({"a": {"b"}, "b": set()}) == ["b", "a"]


# <<< jaunt:derived examples


# >>> jaunt:derived errors
def test_raises_jauntdependencycycleerror():  # derived from: Raises
    with pytest.raises(JauntDependencyCycleError):
        toposort({"a": {"b"}, "b": {"a"}})


# <<< jaunt:derived errors
