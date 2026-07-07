import pathlib
import sys

ROOT = pathlib.Path(__file__).parent
for p in ("engine", "oracle"):
    sys.path.insert(0, str(ROOT / p))
