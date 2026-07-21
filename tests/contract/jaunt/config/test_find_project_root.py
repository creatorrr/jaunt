# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.config:find_project_root
# jaunt:prose-digest=sha256:0a5be7792c7aad6fa0e7625c9b9c675f0c694a4df3d76a895c80ca2236b734c0
# jaunt:signature=d5a6674df4e9cd3f15b7464047d0e71307d471071d9c2d9266dc5edab8b38c5a
# jaunt:body-digest=sha256:1245dc7d28bd1ce14ed03fae7f008ec5f99ae9a42c564b414b28fef77b0903dd
# jaunt:strength=5/14
# jaunt:tool-version=1.7.8
import pytest
from jaunt.config import find_project_root
from jaunt.config import JauntConfigError
from jaunt.config import Path

# >>> jaunt:derived errors
def test_raises_jauntconfigerror():  # derived from: Raises
    with pytest.raises(JauntConfigError):
        find_project_root(Path("/"))
# <<< jaunt:derived errors
