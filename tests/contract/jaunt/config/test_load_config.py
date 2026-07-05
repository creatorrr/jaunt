# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.config:load_config
# jaunt:prose-digest=sha256:7e7b25e80dc50194e3a616f32428d2445802868948407daacee16cbf996b2db5
# jaunt:signature=59628c23a8fdef174b0025e678f45b318a9d5bc3e2e39462cb6a70cfff2c2d21
# jaunt:body-digest=sha256:607a2d80ab6b17e85ab2cf896a3619e6c7a52e50f99b0c8aa2815aaeb3ec61cb
# jaunt:strength=2/431
# jaunt:tool-version=1.5.1
import pytest
from jaunt.config import load_config
from jaunt.config import JauntConfigError
from jaunt.config import Path


# >>> jaunt:derived errors
def test_raises_jauntconfigerror():  # derived from: Raises
    with pytest.raises(JauntConfigError):
        load_config(config_path=Path("/nonexistent/jaunt.toml"))


# <<< jaunt:derived errors
