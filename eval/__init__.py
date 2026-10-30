"""Evaluation harness: E1 chunking, E2 long conversations, E3 requirement edits.

Every experimental setting lives in `eval.settings`; the experiments read it
from there and nowhere else.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = REPO_ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
