# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.config:CodexConfig
# jaunt:prose-digest=sha256:813cb8b7c0bdbb12a18d275744f8cb8e3d72bcfcc713ef246b51890a72422076
# jaunt:signature=60a56581b014d5ed63e58e408f5629d919deeb0973644eafac25bc3d594aba04
# jaunt:body-digest=sha256:28615eaf7e30e025cbb883d1660e79178816cd895477aa47d1c74f1e85cce8a9
# jaunt:strength=8/12
# jaunt:tool-version=1.5.1
import pytest
from jaunt.config import CodexConfig


# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert CodexConfig().model == "gpt-5.5"
    assert CodexConfig().reasoning_effort == "high"
    assert CodexConfig().sandbox == "workspace-write"
    assert CodexConfig().fingerprint_cli_version == False


# <<< jaunt:derived examples
