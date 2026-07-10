# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.config:CodexConfig
# jaunt:prose-digest=sha256:4f78ef9fb6e7554a2aec35a0798e6ea783119470c1c27d582bd541ce3020c91f
# jaunt:signature=60a56581b014d5ed63e58e408f5629d919deeb0973644eafac25bc3d594aba04
# jaunt:body-digest=sha256:6eba55ebd2c515756bfe7d4763a21f0c57cd726c1e5fa50cf8ef87ac8a669e3a
# jaunt:strength=8/12
# jaunt:tool-version=1.6.1
import pytest
from jaunt.config import CodexConfig

# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert CodexConfig().model == "gpt-5.6-sol"
    assert CodexConfig().reasoning_effort == "medium"
    assert CodexConfig().sandbox == "workspace-write"
    assert CodexConfig().fingerprint_cli_version == False
# <<< jaunt:derived examples
