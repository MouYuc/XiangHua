"""Infrastructure layer: the parts of the app that touch the outside world.

Everything here is deliberately dependency-free (stdlib only) and knows nothing
about TTS, HTTP or the product domain:

* ``atomic``    — crash-safe file writes
* ``jsonstore`` — locked, versioned JSON persistence with corrupt-file recovery
* ``logging``   — one structured logger for the whole process

The rule the rest of the codebase follows: **no module writes a JSON file by
hand.** Route through ``jsonstore`` so every writer shares the same lock, the
same temp-file discipline and the same corruption policy.
"""

from __future__ import annotations

from backend.infra.jsonstore import JsonStore, StoreCorruptError

__all__ = ["JsonStore", "StoreCorruptError"]
