import sys
from pathlib import Path

# Make the repo root importable from tests/ (repo modules are not a package).
ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)