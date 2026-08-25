"""Minimal package smoke test (no external deps required)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

def test_package_imports():
    import jobpilot
    assert hasattr(jobpilot, "__version__")
    assert jobpilot.__version__

def test_version_string():
    import jobpilot
    assert isinstance(jobpilot.__version__, str)
    assert jobpilot.__version__  # non-empty
