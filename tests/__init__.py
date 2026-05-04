"""Make ``src/`` importable when running ``python3 -m unittest discover``
from the repository root, without requiring a pip install of the package.
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
