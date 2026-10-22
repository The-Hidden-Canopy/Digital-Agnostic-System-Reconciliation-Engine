import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT / "src", _ROOT / "tools"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
