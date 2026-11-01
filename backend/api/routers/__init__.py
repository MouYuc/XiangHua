"""One module per product area; each exposes a module-level ``router``."""

from __future__ import annotations

from backend.api.routers import (
    dialogue,
    media,
    platform,
    synthesis,
    system,
    timbre,
    training,
    voices,
)

__all__ = [
    "dialogue",
    "media",
    "platform",
    "synthesis",
    "system",
    "timbre",
    "training",
    "voices",
]
