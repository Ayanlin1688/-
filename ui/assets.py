"""Locate shared brand resources in source and PyInstaller installations."""
from pathlib import Path
import sys


def asset_path(name):
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))
    return root / 'assets' / name
