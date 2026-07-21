# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.config:CodexConfig
# jaunt:prose-digest=sha256:a52c72285cb274acec3691bb3686b29fcee6231537675d3165ba7d4da81327eb
# jaunt:signature=177eaf6a83e97b95282473b267973f610f5ec45ea76a42f8b852356462830493
# jaunt:body-digest=sha256:1a542b52cec215ff382e5d0794cffcda259a139b2a85e1d4ca48b79d97245e39
# jaunt:strength=9/13
# jaunt:tool-version=1.7.8
import pytest
from jaunt.config import CodexConfig

# >>> jaunt:derived examples
def test_examples():  # derived from: Examples
    assert CodexConfig().model == "gpt-5.6-sol"
    assert CodexConfig().reasoning_effort == "medium"
    assert CodexConfig().sandbox == "workspace-write"
    assert CodexConfig().quota_wait_minutes == 0.0
    assert CodexConfig().fingerprint_cli_version == False
# <<< jaunt:derived examples
