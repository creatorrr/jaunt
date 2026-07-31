# This contract test battery was derived by jaunt. Derived regions are regenerated; edits outside them are preserved.
# jaunt:derived-from=jaunt.config:load_config
# jaunt:prose-digest=sha256:d7a6989e9a801751d49051fb289011017a38caf4a0188c8f0f1ce50a3ab1a6b4
# jaunt:signature=59628c23a8fdef174b0025e678f45b318a9d5bc3e2e39462cb6a70cfff2c2d21
# jaunt:body-digest=sha256:0eaba3ff5f8df12495e160bf23346810b416ad3036f9b76262c45e493834be58
# jaunt:strength=2/502
# jaunt:tool-version=1.7.14
import pytest
from jaunt.config import load_config
from jaunt.config import JauntConfigError
from jaunt.config import Path

# >>> jaunt:derived errors
def test_raises_jauntconfigerror():  # derived from: Raises
    with pytest.raises(JauntConfigError):
        load_config(config_path=Path("/nonexistent/jaunt.toml"))
# <<< jaunt:derived errors
