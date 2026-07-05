# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.discovery:is_self_module
# jaunt:prose-digest=sha256:00e82257304fb798a54c0bd10729843bb0125ed3d034398f9acc7da7d5b274a6
# jaunt:signature=62f57247628b0e154c736c84e4f7352bca508b2e9bfe6c496a2f0533d1237ce0
# jaunt:body-digest=sha256:ac73a7e0083174fa4181bd251d4ff9a3339e8a8fba35de9cf4a34b7edc6fa12f
# jaunt:strength=5/5
# jaunt:tool-version=1.5.1
import pytest
from jaunt.discovery import is_self_module


# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert is_self_module("jaunt") == True
    assert is_self_module("jaunt.discovery") == True
    assert is_self_module("jaunt.contract.cases") == True
    assert is_self_module("jauntx") == False
    assert is_self_module("os") == False


# <<< jaunt:derived examples
