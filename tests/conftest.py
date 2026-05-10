"""pytest configuration – make the repo root importable so that
``examples/`` can be imported by test_example_agents.py without being
an installed package."""

import sys
from pathlib import Path

# Ensure the repo root (parent of this file's directory) is on sys.path so
# that ``import examples.agents`` works in CI where examples/ is not installed.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
