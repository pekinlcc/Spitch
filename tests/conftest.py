"""Pytest hook: ensure ``src/`` is on ``sys.path`` so ``import spitch`` works
without requiring a pip install. The same trick is applied for the stdlib
``unittest`` runner via ``tests/__init__.py``.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
