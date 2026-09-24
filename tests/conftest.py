"""Keep test runs out of the operator's real Summon state (see _test_state_isolation).

Loaded under a private module name: inventory tests assert that no runtime
script module is imported by this test root.
"""

import importlib.util
import os

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "skills", "summon", "scripts",
                     "_test_state_isolation.py")
_SPEC = importlib.util.spec_from_file_location("summon_test_state_isolation", _PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
_MODULE.isolate()
ISOLATED_STATE = _MODULE.ISOLATED_STATE
