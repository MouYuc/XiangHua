"""Repository-wide path and limit constants.

Extracted verbatim from ``backend.services.core`` so leaf services can read the
repository layout without importing the synthesis facade. ``core`` keeps
re-exporting every name unchanged, so ``core.CONFIG_DIR`` and the
``patch.object(core, "CONFIG_DIR")`` test seam behave exactly as before.

Only pure constants live here. Anything with side effects (locks, stores) or
that belongs to the domain (``SUPPORTED_SYNTHESIS_LANGUAGES``, which is defined
in :mod:`backend.domain.text`) stays where it is.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / ".guangpu_webui"
CONFIG_FILE = CONFIG_DIR / "config.json"
HISTORY_FILE = CONFIG_DIR / "history.json"
OUTPUT_DIR = REPO_ROOT / "output"
REFERENCE_AUDIO_DIR = CONFIG_DIR / "reference_audio"
REFERENCE_AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}
MAX_REFERENCE_AUDIO_BYTES = 100 * 1024 * 1024
MAX_PATCH_BYTES = 1024 * 1024
ORB_COLOR_DEFAULTS = {
    "orb_synthesis_token_color": "#75BBFF",
    "orb_settled_color": "#69DDA4",
    "orb_playback_token_color": "#F8FBFF",
}
TRAINING_AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}
DOWNLOADABLE_SUFFIXES = TRAINING_AUDIO_SUFFIXES | {".zip", ".srt"}
#: On-disk format version for ``config.json``. Bump it and add a migration entry
#: in the config store whenever a persisted field changes meaning.
CONFIG_SCHEMA_VERSION = 1
#: How many synthesis history rows are kept. Older rows are dropped on write —
#: this is a bounded activity log, not an audit trail (provenance sidecars are
#: the durable record).
HISTORY_LIMIT = 200
#: The download registry is a credential map for ``/api/files/<id>``. Entries
#: whose backing file is gone are pruned on write so the map cannot grow forever.
DOWNLOADS_LIMIT = 5000
#: Hard ceiling for a single ffmpeg remux. Generous for a 10-minute take,
#: short enough that a wedged process cannot hold the GPU lease for the session.
FFMPEG_TIMEOUT_SECONDS = 120
