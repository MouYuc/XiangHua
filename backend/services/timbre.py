"""Personal-timbre conversion service (kNN-VC) with job tracking.

This is the "use my own voice" half of the product. Synthesis produces speech in
whatever voice the engine was given; this service moves that speech - or any
other recording, including a sung vocal - into the timbre of a voice profile.

Why this is a separate service instead of another ``core`` function:

* **The model is large and shares one GPU.** WavLM-Large plus its HiFi-GAN
  vocoder is roughly 1.3 GB of weights, and GPT-SoVITS synthesis is usually
  resident on the same 8 GB card. The model is therefore loaded lazily, guarded
  by a lock, and released automatically after an idle period so that a user who
  finishes a conversion is not holding VRAM that the synthesis page needs.
* **Reference audio is expensive to encode, so the result is cached.** Turning a
  voice profile's recordings into a kNN matching pool is the slow part; the pool
  is reused by every conversion for that profile. The cache key includes the
  reference file list with sizes and mtimes, so adding or re-recording audio
  invalidates the pool instead of silently converting against stale frames.
* **Conversion is long enough to need a job.** Encoding several minutes of source
  audio through WavLM-Large takes tens of seconds, so a conversion runs on a
  worker thread and is polled exactly like a synthesis job.

The WebUI never talks to ``scripts/knnvc_runtime`` directly; that module stays
the single place where the vendored upstream code and checkpoints are handled.

The implementation is split behind this facade: reference-corpus and
conversion-source management in ``timbre_refs``, matching-pool preparation in
``timbre_features``, runtime loading / model lifecycle / worker orchestration in
``timbre_worker``. Every historical symbol is re-exported here, so consumers
(``api.deps``, routers, the job registry, tests) keep their import paths.
"""

from __future__ import annotations

import threading

from backend.services.timbre_features import (  # noqa: F401 - facade re-exports
    MAX_REFERENCE_SECONDS,
    TimbreProfileMixin,
)
from backend.services.timbre_refs import (  # noqa: F401 - facade re-exports
    AUDIO_SUFFIXES,
    OUTPUT_ROOT,
    PROFILE_CACHE_DIR,
    REPO_ROOT,
    TIMBRE_DIR,
    corpus_digest,
    disambiguate_labels,
    list_sources,
    reference_files,
    save_source,
    source_dir,
)
from backend.services.timbre_worker import (  # noqa: F401 - facade re-exports
    _CANCELLED_PAYLOAD,
    IDLE_UNLOAD_SECONDS,
    TimbreCancelled,
    TimbreConvertMixin,
    TimbreJobsMixin,
    TimbreModelMixin,
    _Job,
    _load_runtime,
)


class TimbreBackend(TimbreProfileMixin, TimbreConvertMixin, TimbreModelMixin, TimbreJobsMixin):
    """Owns the kNN-VC model, the profile matching pools, and conversion jobs.

    The method groups come from the split modules: profile pool preparation
    (``TimbreProfileMixin``), kNN matching execution (``TimbreConvertMixin``),
    encoder lifecycle (``TimbreModelMixin``) and worker-thread jobs
    (``TimbreJobsMixin``). State stays on the instance/class exactly as before:
    ``_model``/``_model_device``/``_model_lock``/``_last_used`` per instance,
    ``_jobs`` shared process-wide.
    """

    def __init__(self) -> None:
        self._model: object | None = None
        self._model_device: str = ""
        self._model_lock = threading.RLock()
        self._last_used = 0.0


TIMBRE_BACKEND = TimbreBackend()
