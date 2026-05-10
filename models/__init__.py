"""
models/ — Multiplayer-specific data layer.

Solo-game history (matches, rounds, sessions) lives in engine/persistence.py.
This package owns the tables introduced for multiplayer:
  guests     — browser-scoped guest identities (auto-created per visitor)
  accounts   — optional registered accounts (upgrade path from guest)
  mp_matches — multiplayer match records (separate from solo 'matches' table)
"""

from .db import init_models  # noqa: F401
from .guests import ensure_guest, get_guest  # noqa: F401
