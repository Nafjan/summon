"""Keep test runs out of the operator's real Summon state (see _test_state_isolation)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "scripts"))

from _test_state_isolation import isolate  # noqa: E402

isolate()
