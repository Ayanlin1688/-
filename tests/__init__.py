"""Test package compatibility for both unittest discovery and module execution."""
from pathlib import Path
import sys

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
