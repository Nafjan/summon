#!/usr/bin/env python3
"""summon - entry-point shim.

`python summon.py ...` == `python scripts/run_subagent.py ...`
Exists so the quickstart is one obvious command from the repo root.
"""
import os
import runpy
import sys

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "run_subagent.py")

if __name__ == "__main__":
    sys.argv[0] = _SCRIPT
    runpy.run_path(_SCRIPT, run_name="__main__")
