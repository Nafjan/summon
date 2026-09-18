from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _launch_binding import (binding_projection, measure_launch_material,
                             observation, valid_observation, valid_projection)


def test_material_digest_uses_only_adapter_declared_files(tmp_path):
    material = tmp_path / "adapter.js"
    material.write_text("one", encoding="utf-8")
    baseline = measure_launch_material(sys.executable, [str(material)], material_paths=[])
    declared_one = measure_launch_material(
        sys.executable, ["--prompt-file", str(material)], material_paths=[str(material)])
    assert baseline is not None
    assert declared_one is not None
    # Prompt/data-looking argv is not authority unless the adapter explicitly
    # declares that path as launch material.
    material.write_text("two", encoding="utf-8")
    declared_two = measure_launch_material(
        sys.executable, ["--prompt-file", str(material)], material_paths=[str(material)])
    assert measure_launch_material(
        sys.executable, ["--prompt-file", str(material)], material_paths=[]) == baseline
    assert declared_two != declared_one
    assert measure_launch_material(
        sys.executable, [], material_paths=[str(tmp_path / "missing.js")]) is None


def test_projection_validator_is_distinct_from_fresh_observation():
    fresh = observation(sys.executable, ["-c", "pass"], os.getcwd(), os.environ,
                        backend="claude", transport="subprocess")
    assert valid_observation(fresh)
    projected = binding_projection(fresh)
    assert valid_projection(projected)
    projected["executable_content_revision"] = "not-a-digest"
    assert not valid_projection(projected)
