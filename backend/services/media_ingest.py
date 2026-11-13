"""Media intake: upload -> diarization -> slices -> dataset manifest.

The service keeps one JSON session per upload under
``<gpt_sovits_root>/custom_data/media/<session_id>/session.json``. Audio and
derived slices live next to it. Nothing here reaches into the GPT-SoVITS training
pipeline; export produces a standard ``dataset.list`` plus a manifest that can be
consumed later.

Analysis is versioned: every analyze call writes a new ``segments/<run_id>/``
directory and never deletes an older one, because a dataset manifest exported
earlier still points at those slices. A session that predates versioning keeps
its flat ``segments/seg_*.wav`` layout and is exposed as one implicit legacy run;
its slices are never moved or renamed to upgrade it.

Scope is deliberately local and rights-gated. The service does not download from
streaming platforms and does not attempt to clone an unlicensed voice.

This module is now an orchestration facade: the durable session/upload layer
lives in :mod:`backend.services.media_sessions`, and the analysis/dataset
pipeline lives in :mod:`backend.services.media_analysis`. Every historical
public and test-seam symbol is re-exported under its original name, so
``from backend.services.media_ingest import X`` and attribute access keep
working unchanged.
"""

from backend.services.media_analysis import (  # noqa: F401 - facade re-exports
    _ACTIVE_ANALYSES,
    _ANALYSIS_LOCK,
    MediaAnalysisMixin,
    MediaIngestService,
    _list_language,
    _load_whisper,
)
from backend.services.media_sessions import (  # noqa: F401 - facade re-exports
    _PROGRESS,
    _SESSION_LOCKS,
    _SESSION_LOCKS_GUARD,
    ANALYSIS_SAMPLE_RATE,
    DEFAULT_MAX_MEDIA_BYTES,
    EXPORT_SAMPLE_RATE,
    LEGACY_RUN_ID,
    MEDIA_COMMAND_TIMEOUT_SECONDS,
    MEDIA_ROOT_REL,
    SEGMENT_ID_PATTERN,
    SESSION_ID_PATTERN,
    SUPPORTED_MEDIA_SUFFIXES,
    MediaIngestError,
    MediaSessionStore,
    _atomic_json,
    _is_legacy_segment_path,
    _new_run_id,
    _now,
    _replace_with_retry,
    _run_command,
    _sha256_file,
    extract_audio,
    logger,
    max_media_bytes,
    probe_media,
    session_lock,
)
