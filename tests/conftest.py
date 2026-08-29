import sys
from pathlib import Path

# The driver lives in examples/ rather than the package, so make it importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples"))
