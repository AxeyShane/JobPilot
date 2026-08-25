"""Pytest conftest: make the src-layout package importable from the repo root.

The repo uses a src-layout (src/jobpilot), so tests must add src/ to sys.path.
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
