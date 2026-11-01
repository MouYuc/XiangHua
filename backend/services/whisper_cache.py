"""One resident faster-whisper model per process, shared by every ASR path.

Why this module exists
----------------------

Media ingest and single-audio reference preparation each carried their own
``functools.lru_cache`` loader. A large-v3 float16 model plus its CTranslate2
buffers is roughly 2 GB, and the two caches were populated independently: the
second path to run loaded a *second* copy. On an 8 GB card that pushed the process
close to the limit, and Windows (WDDM) answered by spilling into shared memory, so
inference fell to a crawl. Measured in the 2026-11-01 acceptance run: a 6.8 s clip
sat at "语音转写" for over nine minutes with 98% GPU utilisation and 7.9 GB
resident, while each individual transcription is normally a few seconds.

Sharing one loader means exactly one model is resident, and ``TRANSCRIBE_LOCK``
serialises decoding because the underlying CTranslate2 model is not documented as
safe for concurrent ``transcribe`` calls. The lock is taken while the segment
generator is consumed, not while the HTTP request is handled, so a long analysis
still lets the other path queue behind it instead of deadlocking.

Patching seam: callers keep their own module-level ``_load_whisper`` wrapper, so
existing tests can still patch ``media_ingest._load_whisper``.
"""

from __future__ import annotations

import functools
import threading
from pathlib import Path
from typing import Any

# CTranslate2 models are cheap to keep but expensive to duplicate; one slot forces
# a previous language/model to be released before another is loaded.
CACHE_SIZE = 1

# Serialises ``transcribe`` on the shared model (see module docstring).
TRANSCRIBE_LOCK = threading.Lock()


@functools.lru_cache(maxsize=CACHE_SIZE)
def load_whisper(model_name: str):
    """Return the cached ``faster_whisper.WhisperModel`` for ``model_name``."""
    from faster_whisper import WhisperModel
    from scripts.run_whisper import resolve_local_model

    local = resolve_local_model(model_name)
    model_ref = str(local) if local is not None else model_name
    try:
        import torch

        device = "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        device = "cpu"
    compute_type = "float16" if device == "cuda" else "int8"
    return WhisperModel(model_ref, device=device, compute_type=compute_type)


def transcribe(
    path: str | Path,
    *,
    model: str = "large-v3",
    language: str | None = "zh",
    beam_size: int = 1,
    vad_filter: bool = False,
    loader: Any | None = None,
) -> tuple[str, Any]:
    """Transcribe one file and return ``(text, info)``.

    ``loader`` lets a caller keep its own patchable ``_load_whisper`` seam; when it
    is omitted the shared cache is used.
    """
    whisper = (loader or load_whisper)(model)
    with TRANSCRIBE_LOCK:
        segments, info = whisper.transcribe(
            str(path),
            beam_size=beam_size,
            vad_filter=vad_filter,
            language=language,
        )
        # Consumed inside the lock on purpose: the generator drives the model.
        text = "".join(segment.text for segment in segments)
    return text, info
