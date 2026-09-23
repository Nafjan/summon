"""The pytest roots must keep dispatcher state out of the operator's home."""

from __future__ import annotations

import os
from pathlib import Path

from _test_state_isolation import ISOLATED_STATE


def test_conftest_redirects_every_home_state_override():
    home = (Path.home() / ".agents").resolve()
    for name in ISOLATED_STATE:
        assert name in os.environ, name
        assert home not in Path(os.environ[name]).resolve().parents, name
