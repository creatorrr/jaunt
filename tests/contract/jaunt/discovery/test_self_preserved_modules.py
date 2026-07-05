# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.discovery:self_preserved_modules
# jaunt:prose-digest=sha256:510aab2fe3c4075683adb5dbecc749ded488da44f8d4e7e9e6005cb18bb64d48
# jaunt:signature=5dd0141a57e9b25a41911c00a7d01174adf7aa908791631efb7a8593e32f6e1b
# jaunt:body-digest=sha256:3f5c97106d429ed6a3a3127d584d2150e86fec5c29adbebfa1ae9aa6212e4941
# jaunt:strength=3/3
# jaunt:tool-version=1.5.1
import pytest
from jaunt.discovery import self_preserved_modules


# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert self_preserved_modules([]) == frozenset()
    assert self_preserved_modules(["os", "collections"]) == frozenset()
    assert self_preserved_modules(["jaunt.discovery"]) == frozenset({"jaunt.discovery"})


# <<< jaunt:derived examples
