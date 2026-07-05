# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.cache:CacheEntry
# jaunt:prose-digest=sha256:5bff76da2724384b5de70227c25bbebb59602ceb71eedb988075a6c4637cadf2
# jaunt:signature=0df5198960b43dbd85577e73495fa9bdeb95aca91e09cdb3eb083476eff11c77
# jaunt:body-digest=sha256:3792bf8d6d972c6a96cbac13d5cac0337897ad3ab4c8f48830fc5fc046c9e47e
# jaunt:strength=6/9
# jaunt:tool-version=1.5.1
import pytest
from jaunt.cache import CacheEntry


# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert CacheEntry("body", 10, 20, "gpt-5.5", "openai", 0.0).source == "body"
    assert CacheEntry("body", 10, 20, "gpt-5.5", "openai", 0.0).prompt_tokens == 10
    assert CacheEntry("body", 10, 20, "gpt-5.5", "openai", 0.0).provider == "openai"


# <<< jaunt:derived examples
