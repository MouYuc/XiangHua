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
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np
import soundfile as sf

from backend.infra.atomic import atomic_write_text
from backend.infra.logging import get_logger
from backend.services import media_diarization, reference_audio, whisper_cache
from backend.services.gpu_arbiter import GPU_ARBITER

MEDIA_ROOT_REL = Path("custom_data") / "media"
ANALYSIS_SAMPLE_RATE = 16000
EXPORT_SAMPLE_RATE = 32000

# Pre-versioning sessions stored slices flat in ``segments/seg_*.wav``. They are
# surfaced as this synthetic run so export/activate can treat old and new
# sessions uniformly without touching the files on disk.
LEGACY_RUN_ID = "run_legacy"
DATASET_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,120}$")
SEGMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

SUPPORTED_MEDIA_SUFFIXES = {
    ".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac",
    ".mp4", ".mkv", ".mov", ".webm", ".avi",
}

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

logger = get_logger("xianghua.media")
_PROGRESS = Callable[[float, str], None]

DEFAULT_MAX_MEDIA_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB：够长视频，又不至于灌满磁盘


def max_media_bytes() -> int:
    """Upload cap for one media file; ``XIANGHUA_MAX_MEDIA_BYTES`` overrides it."""
    raw = os.environ.get("XIANGHUA_MAX_MEDIA_BYTES")
    if not raw:
        return DEFAULT_MAX_MEDIA_BYTES
    try:
        return max(1024 * 1024, int(raw))
    except ValueError:
        return DEFAULT_MAX_MEDIA_BYTES

# ``_media_service()`` in app.py builds a fresh MediaIngestService for every
# request, so the per-instance lock alone cannot stop two concurrent analyze
# requests from both starting a worker (and loading ERes2NetV2 twice). This
# registry is process-wide on purpose: check, mark job=running, and register must
# happen under one lock, and the entry must be removed when the worker ends.
_ACTIVE_ANALYSES: dict[str, str] = {}
_ANALYSIS_LOCK = threading.Lock()

# One lock per ``session.json``, process-wide. A fresh MediaIngestService is
# built per request (see ``_media_service`` in app.py), so an instance-level lock
# would serialise nothing: two requests would each hold their own.
_SESSION_LOCKS: dict[str, threading.RLock] = {}
_SESSION_LOCKS_GUARD = threading.Lock()


def session_lock(path: Path) -> threading.RLock:
    """The process-wide lock guarding one session file."""
    key = str(path.resolve())
    with _SESSION_LOCKS_GUARD:
        existing = _SESSION_LOCKS.get(key)
        if existing is None:
            existing = threading.RLock()
            _SESSION_LOCKS[key] = existing
        return existing


class MediaIngestError(RuntimeError):
    """A user-actionable intake failure."""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _new_run_id() -> str:
    """A sortable, collision-resistant analysis version id."""
    return f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"


def _is_legacy_segment_path(segment: dict[str, Any]) -> bool:
    """True for a flat ``segments/seg_XXXX.wav`` path (pre-versioning layout)."""
    relative = str(segment.get("path") or "").replace("\\", "/").strip("/")
    parts = relative.split("/")
    return len(parts) == 2 and parts[0] == "segments" and parts[1].startswith("seg_")


def _replace_with_retry(temporary: Path, target: Path, attempts: int = 12) -> None:
    """Move a finished temp file into place, retrying a Windows sharing violation.

    ``os.replace`` fails with "access denied" while another thread still holds the
    target open for reading, because Python's ``open`` does not share deletion. An
    analysis progress write must not fail just because the UI polled the session
    file at the same moment, so retry briefly with a backoff instead.
    """
    delay = 0.01
    for attempt in range(attempts):
        try:
            temporary.replace(target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2.0, 0.15)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A unique temp name matters when two writers race: a shared "session.json.tmp"
    # would let one writer publish the other's half-flushed payload.
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        _replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


#: Ceiling for one ffprobe/ffmpeg invocation. A 2 GiB upload decodes in well
#: under this; the timeout exists so a wedged child cannot pin a request thread
#: (uploads run in the threadpool) or the analysis worker forever.
MEDIA_COMMAND_TIMEOUT_SECONDS = 1800


def _run_command(command: list[str], *, timeout: float = MEDIA_COMMAND_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise MediaIngestError(
            "找不到 ffmpeg/ffprobe。请先安装 FFmpeg，并确认它在 PATH 中。"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaIngestError(
            f"媒体处理超时（{int(timeout)} 秒）：{Path(command[-1]).name}"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise MediaIngestError(f"媒体处理命令失败：{detail[-900:]}")
    return completed


def probe_media(path: Path) -> dict[str, Any]:
    """Read duration and audio stream information with ffprobe."""
    completed = _run_command(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name,sample_rate,channels",
            "-show_entries", "format=duration",
            "-of", "json",
            str(path),
        ]
    )
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MediaIngestError("ffprobe 返回了无法解析的结果。") from exc
    streams = payload.get("streams") or []
    if not streams:
        raise MediaIngestError("这个文件里没有音轨。")
    stream = streams[0]
    try:
        duration = float((payload.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "duration_seconds": round(duration, 3),
        "codec": stream.get("codec_name") or "",
        "sample_rate": int(stream.get("sample_rate") or 0),
        "channels": int(stream.get("channels") or 0),
        "size_bytes": path.stat().st_size,
    }


def extract_audio(source: Path, destination: Path, rate: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run_command(
        [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(source),
            "-vn", "-ac", "1", "-ar", str(rate),
            "-c:a", "pcm_s16le",
            str(destination),
        ]
    )
    if not destination.is_file() or destination.stat().st_size == 0:
        raise MediaIngestError("音轨提取失败：输出文件为空。")


def _load_whisper(model_name: str):
    """Patchable seam; the model itself is cached process-wide in ``whisper_cache``.

    A private ``lru_cache`` here duplicated the large-v3 model whenever reference
    preparation had already loaded it (see ``whisper_cache`` module docstring).
    """
    return whisper_cache.load_whisper(model_name)


class MediaIngestService:
    """Owns media sessions and their derived artifacts."""

    def __init__(
        self,
        root: str | Path,
        *,
        embedder: Any | None = None,
        checkpoint: str | Path | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.base = self.root / MEDIA_ROOT_REL
        self.base.mkdir(parents=True, exist_ok=True)
        self._embedder = embedder
        self._checkpoint = Path(checkpoint) if checkpoint else None
        self._lock = threading.RLock()
        self._embed_lock = threading.Lock()

    # ------------------------------------------------------------------ paths ---

    def session_dir(self, session_id: str) -> Path:
        if not SESSION_ID_PATTERN.fullmatch(str(session_id or "")):
            raise MediaIngestError("非法的采集会话 ID。")
        directory = (self.base / session_id).resolve()
        if self.base.resolve() not in directory.parents:
            raise MediaIngestError("采集会话路径越界。")
        return directory

    def _absolute(self, session_id: str, relative: str) -> Path:
        directory = self.session_dir(session_id)
        target = (directory / relative).resolve()
        if target != directory and directory not in target.parents:
            raise MediaIngestError("会话文件路径越界。")
        return target

    def _relative(self, session_id: str, path: Path) -> str:
        return path.resolve().relative_to(self.session_dir(session_id)).as_posix()

    # --------------------------------------------------------------- storage ---

    def create_session(
        self,
        name: str = "",
        *,
        language: str = "zh",
        rights_declared: bool = False,
        rights_note: str = "",
    ) -> dict[str, Any]:
        if not rights_declared:
            raise MediaIngestError("上传前必须确认你有权将该素材用于训练。")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_id = f"media_{stamp}_{uuid.uuid4().hex[:6]}"
        directory = self.session_dir(session_id)
        directory.mkdir(parents=True, exist_ok=False)
        session = {
            "schema_version": 1,
            "session_id": session_id,
            "name": (name or "").strip() or "未命名采集",
            "language": language or "zh",
            "created_at": _now(),
            "updated_at": _now(),
            "rights": {
                "declared": True,
                "note": (rights_note or "").strip(),
                "declared_at": _now(),
            },
            "source": None,
            "job": {"status": "idle", "stage": "等待上传", "progress": 0.0, "message": "", "error": ""},
            "speakers": [],
            "segments": [],
            "runs": [],
            "active_run": "",
            "selected_speaker": "",
            "dataset": None,
        }
        self.save(session)
        return session

    def save(self, session: dict[str, Any]) -> None:
        session["updated_at"] = _now()
        path = self.session_dir(session["session_id"]) / "session.json"
        with session_lock(path):
            _atomic_json(path, session)

    def patch(
        self,
        session_id: str,
        mutator: Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> dict[str, Any]:
        """Locked read-modify-write against the *current* session on disk.

        A long analysis used to ``load()`` once at the start and ``save()`` the
        whole dict minutes later, silently reverting anything the user changed in
        the meantime (a chosen speaker, an exported dataset, a corrected
        transcript). Callers now hand over only the fields they own; this method
        re-reads under the session lock so unrelated fields always survive.
        """
        path = self.session_dir(session_id) / "session.json"
        with session_lock(path):
            session = self.load(session_id)
            updated = mutator(session)
            if updated is None:
                return session
            updated["updated_at"] = _now()
            _atomic_json(path, updated)
            return updated

    def load(self, session_id: str) -> dict[str, Any]:
        path = self.session_dir(session_id) / "session.json"
        if not path.is_file():
            raise MediaIngestError("采集会话不存在。")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MediaIngestError("采集会话数据损坏。") from exc
        if payload.get("session_id") != session_id:
            raise MediaIngestError("采集会话 ID 不匹配。")
        # Old sessions have no runs/active_run; derive the implicit legacy run in
        # memory. Loading must stay read-only: no file is moved or rewritten.
        self._hydrate_runs(payload)
        return payload

    def list_sessions(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in sorted(self.base.glob("*/session.json")):
            try:
                session = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            rows.append(
                {
                    "session_id": session.get("session_id", path.parent.name),
                    "name": session.get("name", ""),
                    "created_at": session.get("created_at", ""),
                    "updated_at": session.get("updated_at", ""),
                    "language": session.get("language", "zh"),
                    "source": session.get("source"),
                    "job": session.get("job", {}),
                    "speaker_count": len(session.get("speakers") or []),
                    "segment_count": len(session.get("segments") or []),
                    "selected_speaker": session.get("selected_speaker", ""),
                    "dataset": session.get("dataset"),
                }
            )
        rows.sort(key=lambda row: row.get("updated_at") or "", reverse=True)
        return rows

    # ------------------------------------------------------------------- runs ---

    def _hydrate_runs(self, session: dict[str, Any]) -> dict[str, Any]:
        """Expose a pre-versioning session as one implicit legacy run.

        Called from :meth:`load`. Sessions written before analysis versioning
        keep their slices flat in ``segments/seg_*.wav`` and have no ``runs``
        metadata. Deriving ``run_legacy`` here keeps export/activate uniform
        without moving files or saving anything: the on-disk session.json stays
        exactly as the old code wrote it until the caller performs an action.
        """
        runs = session.get("runs")
        if not isinstance(runs, list):
            runs = []
        session["runs"] = runs
        active = str(session.get("active_run") or "")
        known = {str(run.get("run_id") or "") for run in runs if isinstance(run, dict)}
        if runs:
            if active not in known:
                last = runs[-1] if isinstance(runs[-1], dict) else {}
                session["active_run"] = str(last.get("run_id") or "")
            return session

        segments = session.get("segments")
        if not isinstance(segments, list) or not segments:
            session["active_run"] = active
            return session
        flat_segments = [segment for segment in segments if isinstance(segment, dict)]
        if not flat_segments or not all(_is_legacy_segment_path(segment) for segment in flat_segments):
            session["active_run"] = active
            return session
        runs.append(self._legacy_run_metadata(session, flat_segments))
        session["active_run"] = LEGACY_RUN_ID
        return session

    @staticmethod
    def _legacy_run_metadata(session: dict[str, Any], segments: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "run_id": LEGACY_RUN_ID,
            "created_at": str(session.get("updated_at") or session.get("created_at") or ""),
            "speaker_count": len(session.get("speakers") or []),
            "segment_count": len(segments),
            "duration_seconds": round(
                sum(float(segment.get("duration_seconds") or 0.0) for segment in segments if isinstance(segment, dict)),
                3,
            ),
            "path": "segments",
        }

    @staticmethod
    def _active_legacy_segments(session: dict[str, Any]) -> list[dict[str, Any]]:
        """The flat legacy slices that are still the active analysis, if any."""
        if str(session.get("active_run") or "") != LEGACY_RUN_ID:
            return []
        segments = [segment for segment in session.get("segments") or [] if isinstance(segment, dict)]
        if not segments or not all(_is_legacy_segment_path(segment) for segment in segments):
            return []
        return segments

    def _snapshot_legacy_segments(self, session: dict[str, Any], segments: list[dict[str, Any]]) -> None:
        """Persist the legacy segment list next to its slices before it is replaced.

        Best-effort by design: a legacy session must keep analyzing even if the
        snapshot cannot be written, and the run stays exportable while it is the
        active run.
        """
        try:
            directory = self.session_dir(session["session_id"]) / "segments"
            target = directory / "segments.json"
            if target.is_file():
                return
            directory.mkdir(parents=True, exist_ok=True)
            _atomic_json(target, [dict(segment) for segment in segments])
        except (OSError, MediaIngestError):
            return

    def _resolve_run_segments(
        self, session: dict[str, Any], run_id: str | None
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """Pick the analysis version to read and return its segment list."""
        runs = [run for run in session.get("runs") or [] if isinstance(run, dict)]
        if run_id:
            for run in runs:
                if str(run.get("run_id") or "") == str(run_id):
                    return run, self._read_run_segments(session, run)
            raise MediaIngestError("指定的分析版本不存在。")
        active = str(session.get("active_run") or "")
        run = next((item for item in runs if str(item.get("run_id") or "") == active), None)
        if run is None and runs:
            run = runs[-1]
        if run is None:
            # A payload without any run metadata (very old or partial session)
            # still exports its current segment list rather than failing.
            segments = [segment for segment in session.get("segments") or [] if isinstance(segment, dict)]
            if segments:
                return {"run_id": "", "path": ""}, segments
            return None, []
        return run, self._read_run_segments(session, run)

    def _read_run_segments(self, session: dict[str, Any], run: dict[str, Any]) -> list[dict[str, Any]]:
        """Read ``segments/<run_id>/segments.json``, falling back to the session.

        The snapshot is what a run looked like at analysis time (transcripts
        included), so exporting an old run is not affected by a later re-run.
        """
        session_id = str(session["session_id"])
        relative = str(run.get("path") or "")
        run_id = str(run.get("run_id") or "")
        live = [segment for segment in session.get("segments") or [] if isinstance(segment, dict)]
        # The active run's live list is what the current API edits (a manual
        # transcript fix mutates session["segments"]), so keep reading it to
        # preserve existing behavior. Runs superseded by a newer analysis are
        # read from their frozen segments.json instead.
        if run_id and run_id == str(session.get("active_run") or "") and live:
            return live
        if relative:
            snapshot = self._absolute(session_id, relative) / "segments.json"
            if snapshot.is_file():
                try:
                    payload = json.loads(snapshot.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise MediaIngestError("分析版本的切片清单损坏。") from exc
                if isinstance(payload, list):
                    return payload
                if isinstance(payload, dict) and isinstance(payload.get("segments"), list):
                    return payload["segments"]
                raise MediaIngestError("分析版本的切片清单格式不正确。")
        if run_id and run_id == str(session.get("active_run") or ""):
            return live
        if run_id == LEGACY_RUN_ID:
            legacy = [segment for segment in session.get("segments") or [] if isinstance(segment, dict)]
            if legacy and all(_is_legacy_segment_path(segment) for segment in legacy):
                return legacy
        raise MediaIngestError("这个分析版本没有保存切片清单，无法导出。")

    # ----------------------------------------------------------------- upload ---

    def ingest_file(
        self,
        session_id: str,
        filename: str,
        stream: BinaryIO,
        *,
        background: bool = False,
        autostart_analysis: bool = False,
        speaker_count: int | None = None,
        transcribe: bool = False,
    ) -> dict[str, Any]:
        """Store one uploaded recording.

        ``background=False`` (default) waits for probe + transcode and returns the
        ready session — the behaviour CLI callers and tests rely on.
        ``background=True`` returns as soon as the bytes are on disk, with
        ``job.status == "uploading"``; the HTTP route uses this so a slow decode
        cannot pin a request-thread worker.
        """
        try:
            return self._ingest_file(
                session_id,
                filename,
                stream,
                background=background,
                autostart_analysis=autostart_analysis,
                speaker_count=speaker_count,
                transcribe=transcribe,
            )
        except BaseException:
            self._discard_session_without_source(session_id)
            raise

    def _discard_session_without_source(self, session_id: str) -> None:
        """Remove the empty session shell left behind by a failed first upload.

        ``create_session`` writes ``session.json`` before the upload is validated,
        so a rejected suffix (or any ffmpeg/IO failure) would otherwise leave a
        source-less row in ``list_sessions``. Sessions that already carry a source
        are never deleted. Cleanup is best-effort: the original exception wins.
        """
        try:
            session = self.load(session_id)
        except MediaIngestError:
            return
        if session.get("source"):
            return
        try:
            shutil.rmtree(self.session_dir(session_id), ignore_errors=True)
        except MediaIngestError:
            return

    def _ingest_file(
        self,
        session_id: str,
        filename: str,
        stream: BinaryIO,
        *,
        background: bool = False,
        autostart_analysis: bool = False,
        speaker_count: int | None = None,
        transcribe: bool = False,
    ) -> dict[str, Any]:
        session = self.load(session_id)
        if not session.get("rights", {}).get("declared"):
            raise MediaIngestError("该会话没有权利声明，不能上传媒体。")
        if session.get("source"):
            raise MediaIngestError("该会话已经有媒体文件，请新建一个采集会话再上传。")

        safe_name = Path(str(filename or "media")).name
        suffix = Path(safe_name).suffix.lower()
        if suffix not in SUPPORTED_MEDIA_SUFFIXES:
            raise MediaIngestError(f"不支持的文件类型：{suffix or '未知'}。")
        directory = self.session_dir(session_id)
        source_path = directory / "source" / safe_name
        source_path.parent.mkdir(parents=True, exist_ok=True)
        # 有上限的流式落盘：媒体上传没有参考音的 100MB 规则，但也不能无限。
        limit = max_media_bytes()
        written = 0
        try:
            with source_path.open("wb") as output:
                while chunk := stream.read(1024 * 1024):
                    written += len(chunk)
                    if written > limit:
                        raise MediaIngestError(
                            f"上传的媒体不能超过 {limit // (1024 * 1024)} MB。"
                        )
                    output.write(chunk)
        except BaseException:
            source_path.unlink(missing_ok=True)
            raise
        if source_path.stat().st_size == 0:
            source_path.unlink(missing_ok=True)
            raise MediaIngestError("上传的文件为空。")

        if not background:
            # Synchronous path: callers that need the finished session in hand
            # (CLI, tests, scripts) keep the old behaviour, including letting an
            # I/O failure propagate so the half-built session is discarded.
            self._prepare_media_sync(session_id, source_path, safe_name)
            return self.load(session_id)

        # Probing and decoding a two-hour recording takes minutes. Doing it in
        # the request thread occupied a pool worker for the whole upload, so a
        # few slow files could exhaust the pool and freeze every other endpoint.
        # The bytes are already on disk, so hand the CPU work to a worker and let
        # the UI poll `job.status == "uploading"`.
        session["job"] = {
            "status": "uploading",
            "stage": "转码中",
            "progress": 0.0,
            "message": "正在读取媒体信息并转码。",
            "error": "",
        }
        self.save(session)
        threading.Thread(
            target=self._prepare_uploaded_media,
            args=(session_id, source_path, safe_name),
            kwargs={
                "autostart_analysis": autostart_analysis,
                "speaker_count": speaker_count,
                "transcribe": transcribe,
            },
            daemon=True,
            name=f"media-prepare-{session_id}",
        ).start()
        return session

    def _prepare_uploaded_media(
        self,
        session_id: str,
        source_path: Path,
        safe_name: str,
        *,
        autostart_analysis: bool = False,
        speaker_count: int | None = None,
        transcribe: bool = False,
    ) -> None:
        """Worker-thread wrapper: prepare, and record any failure on the session.

        The synchronous path calls :meth:`_prepare_media_sync` directly so an
        I/O failure still propagates and the half-built session is discarded.
        """
        try:
            self._prepare_media_sync(session_id, source_path, safe_name)
        except Exception as exc:
            logger.error("媒体转码失败：%s", exc)
            error_text = str(exc)
            self.patch(
                session_id,
                lambda fresh: {
                    **fresh,
                    "job": {
                        "status": "fail",
                        "stage": "转码失败",
                        "progress": 0.0,
                        "message": "媒体转码失败。",
                        "error": error_text,
                    },
                },
            )
            return
        if autostart_analysis:
            try:
                self.start_analysis(session_id, speaker_count=speaker_count, transcribe=transcribe)
            except MediaIngestError as exc:
                message = str(exc)
                self.patch(
                    session_id,
                    lambda fresh, message=message: {
                        **fresh,
                        "job": {
                            "status": "fail",
                            "stage": "无法开始分析",
                            "progress": 0.0,
                            "message": message,
                            "error": message,
                        },
                    },
                )

    def _prepare_media_sync(self, session_id: str, source_path: Path, safe_name: str) -> None:
        """Probe + transcode one uploaded file and mark the session ready. Raises on failure."""
        directory = self.session_dir(session_id)
        probe = probe_media(source_path)
        analysis_path = directory / "analysis" / "source_16k.wav"
        export_path = directory / "analysis" / "source_32k.wav"
        extract_audio(source_path, analysis_path, ANALYSIS_SAMPLE_RATE)
        extract_audio(source_path, export_path, EXPORT_SAMPLE_RATE)
        source = {
            "filename": safe_name,
            "path": self._relative(session_id, source_path),
            "analysis_audio": self._relative(session_id, analysis_path),
            "export_audio": self._relative(session_id, export_path),
            "duration_seconds": probe["duration_seconds"],
            "codec": probe["codec"],
            "sample_rate": probe["sample_rate"],
            "channels": probe["channels"],
            "size_bytes": probe["size_bytes"],
            "sha256": _sha256_file(source_path),
        }

        def apply_ready(fresh: dict[str, Any]) -> dict[str, Any]:
            return {
                **fresh,
                "source": source,
                "job": {
                    "status": "idle",
                    "stage": "等待分析",
                    "progress": 0.0,
                    "message": "媒体已入库，可以开始说话人分离。",
                    "error": "",
                },
                "speakers": [],
                "segments": [],
                "runs": [],
                "active_run": "",
                "selected_speaker": "",
                "dataset": None,
            }

        self.patch(session_id, apply_ready)

    # -------------------------------------------------------------- diarize ---

    def _embed(self, audio: np.ndarray, rate: int) -> np.ndarray:
        if self._embedder is None:
            with self._embed_lock:
                if self._embedder is None:
                    from scripts.speaker_embedding import SpeakerEmbedder

                    self._embedder = SpeakerEmbedder(self._checkpoint)
        return self._embedder.embed_audio(audio, rate)

    def analyze(
        self,
        session_id: str,
        *,
        speaker_count: int | None = None,
        transcribe: bool = False,
        model: str = "large-v3",
        progress: _PROGRESS | None = None,
    ) -> dict[str, Any]:
        session = self.load(session_id)
        source = session.get("source") or {}
        if not source.get("analysis_audio"):
            # Distinguish "still transcoding" from "nothing uploaded": the upload
            # endpoint now returns before the decode finishes.
            if (session.get("job") or {}).get("status") == "uploading":
                raise MediaIngestError("媒体仍在转码中，请等待转码完成后再开始分析。")
            raise MediaIngestError("还没有上传媒体。")

        def notify(fraction: float, stage: str) -> None:
            if progress is not None:
                progress(max(0.0, min(1.0, fraction)), stage)

        # Analysis loads ERes2NetV2 (and optionally Whisper), so it holds the
        # process-wide GPU lease for its entire run. ``timeout=None`` is
        # deliberate: a training or feature-extraction job ahead of it can hold
        # the card for minutes, and this long task should wait rather than fail
        # halfway through a movie.
        with GPU_ARBITER.lease(
            "media_analysis",
            job_id=session_id,
            label=f"媒体分析 {session.get('name') or session_id}",
            timeout=None,
        ):
            notify(0.02, "读取音轨")
            audio, rate = sf.read(
                str(self._absolute(session_id, source["analysis_audio"])), dtype="float32", always_2d=True
            )
            mono = audio.mean(axis=1) if audio.shape[1] > 1 else audio[:, 0]
            mono = np.ascontiguousarray(mono, dtype=np.float32)

            notify(0.08, "检测语音")
            result = media_diarization.diarize(
                mono,
                int(rate),
                self._embed,
                speaker_count=speaker_count,
                progress=lambda value: notify(0.08 + 0.55 * value, "说话人分离"),
            )
            if not result.turns:
                raise MediaIngestError("没有检测到可用语音；请检查音量、静音阈值或文件是否只有音乐。")

            notify(0.65, "导出切片")
            export_audio, export_rate = sf.read(
                str(self._absolute(session_id, source["export_audio"])), dtype="float32", always_2d=True
            )
            export_mono = export_audio.mean(axis=1) if export_audio.shape[1] > 1 else export_audio[:, 0]
            export_mono = np.ascontiguousarray(export_mono, dtype=np.float32)
            duration = export_mono.size / export_rate if export_rate else 0.0

            # Every analysis gets its own immutable directory. Re-running must
            # never delete the previous slices: exported manifests still point
            # at them, and activating an old dataset later relies on them.
            run_id = _new_run_id()
            run_relative = f"segments/{run_id}"
            segments_dir = self.session_dir(session_id) / "segments" / run_id
            segments_dir.mkdir(parents=True, exist_ok=False)

            segments: list[dict[str, Any]] = []
            counter = 0
            total_turns = max(1, len(result.turns))
            for turn_index, turn in enumerate(result.turns):
                chunks = media_diarization.split_turn(export_mono, int(export_rate), turn)
                for start, end in chunks:
                    pre, post = media_diarization.segment_bounds(start, end, duration)
                    chunk = export_mono[int(pre * export_rate): int(post * export_rate)]
                    if chunk.size < int(0.2 * export_rate):
                        continue
                    counter += 1
                    segment_id = f"seg_{counter:04d}"
                    path = segments_dir / f"{segment_id}.wav"
                    sf.write(str(path), chunk, int(export_rate), subtype="PCM_16")
                    qc = self._analyze_qc(path)
                    segments.append(
                        {
                            "segment_id": segment_id,
                            "speaker_id": f"spk_{turn.speaker_index + 1:02d}",
                            "start": round(float(start), 3),
                            "end": round(float(end), 3),
                            "duration_seconds": round(float(end - start), 3),
                            "path": self._relative(session_id, path),
                            "audio_sha256": _sha256_file(path),
                            "text": "",
                            "qc": qc,
                        }
                    )
                notify(0.65 + 0.25 * ((turn_index + 1) / total_turns), "导出切片")

            if transcribe and segments:
                notify(0.9, "语音转写")
                self._transcribe(session_id, segments, model=model, language=session.get("language", "zh"))

            speaker_rows = [asdict(speaker) for speaker in result.speakers]
            for row in speaker_rows:
                row["segment_count"] = sum(
                    1 for segment in segments if segment.get("speaker_id") == row["speaker_id"]
                )

            # Freeze this run's view (transcripts included) next to its slices.
            # Exporting the run later reads this file instead of live state.
            _atomic_json(segments_dir / "segments.json", segments)

            # A pre-versioning session's only slices are about to stop being
            # ``session["segments"]``. Snapshot them as run_legacy first so the
            # old analysis stays exportable. Files themselves are untouched.
            legacy_segments = self._active_legacy_segments(session)
            if legacy_segments:
                self._snapshot_legacy_segments(session, legacy_segments)

            runs = [item for item in session.get("runs") or [] if isinstance(item, dict)]
            runs.append(
                {
                    "run_id": run_id,
                    "created_at": _now(),
                    "speaker_count": len(result.speakers),
                    "segment_count": len(segments),
                    "duration_seconds": round(
                        sum(float(segment.get("duration_seconds") or 0.0) for segment in segments), 3
                    ),
                    "path": run_relative,
                }
            )
            session["runs"] = runs
            session["active_run"] = run_id
            # Kept in sync for the existing API/frontend: ``segments`` is always
            # the active run. Historical runs live in their segments.json.
            session["segments"] = segments
            session["speakers"] = speaker_rows
            session["selected_speaker"] = result.speakers[0].speaker_id if result.speakers else ""
            session["job"] = {
                "status": "done",
                "stage": "完成",
                "progress": 1.0,
                "message": f"识别到 {len(result.speakers)} 个说话人，生成 {len(segments)} 个切片。",
                "error": "",
            }
            # Patch only the fields this analysis owns. The old code saved the
            # whole dict loaded minutes earlier, which reverted a speaker choice
            # or mid-run export the user made while it was running.
            #
            # ``dataset`` is deliberately cleared: it points at slices from the
            # *previous* run, so keeping it after a re-analysis would hand the
            # next export a stale manifest (documented contract, pinned by
            # test_activate_dataset_by_id_after_reanalysis_replays_the_old_manifest).
            session = self.patch(
                session_id,
                lambda fresh: {
                    **fresh,
                    "runs": runs,
                    "active_run": run_id,
                    "segments": segments,
                    "speakers": speaker_rows,
                    "selected_speaker": session["selected_speaker"],
                    "dataset": None,
                    "job": session["job"],
                },
            )
            notify(1.0, "完成")
        return session

    def _analyze_qc(self, path: Path) -> dict[str, Any]:
        try:
            report = reference_audio.analyze(path, purpose="synthesis").to_dict()
        except Exception as exc:  # QC must never discard a usable slice.
            return {"status": "warn", "blocking": [], "warnings": [f"质检失败：{exc}"], "metrics": {}}
        return {
            "status": report["status"],
            "blocking": report["blocking"],
            "warnings": report["warnings"],
            "metrics": report["metrics"],
        }

    def _transcribe(
        self,
        session_id: str,
        segments: list[dict[str, Any]],
        *,
        model: str,
        language: str,
    ) -> None:
        # Local import: voice_ingest is a sibling service that imports the same
        # diarization/audio helpers. Keeping this import inside the call site
        # avoids a module-level dependency (and any future import cycle) while
        # still sharing the exact OpenCC t2s normalisation used by the main line.
        try:
            whisper = _load_whisper(model)
            from backend.services.voice_ingest import normalize_transcript
        except Exception as exc:
            for segment in segments:
                segment["text_error"] = f"转写不可用：{exc}"
            return

        whisper_language = "zh" if language == "yue" else language
        for segment in segments:
            path = self._absolute(session_id, segment["path"])
            try:
                raw_text, _info = whisper_cache.transcribe(
                    path,
                    model=model,
                    language=whisper_language,
                    beam_size=1,
                    loader=lambda _name: whisper,
                )
                text = normalize_transcript(raw_text, language=language)
                # Normalise against the session language, not the whisper hint:
                # Cantonese keeps its traditional characters ("係" is not "系"),
                # so the main line passes "yue" through unchanged as well.
                segment["text"] = text
            except Exception as exc:
                segment["text_error"] = str(exc)

    def start_analysis(
        self,
        session_id: str,
        *,
        speaker_count: int | None = None,
        transcribe: bool = False,
        model: str = "large-v3",
    ) -> dict[str, Any]:
        token = uuid.uuid4().hex
        with _ANALYSIS_LOCK:
            with self._lock:
                session = self.load(session_id)
                job = session.get("job") or {}
                if session_id in _ACTIVE_ANALYSES or job.get("status") == "running":
                    raise MediaIngestError("这个会话已经在分析中。")
                # The registry entry is written in the same critical section as
                # the job=running marker, so a second request from another
                # per-request service instance cannot slip through.
                _ACTIVE_ANALYSES[session_id] = token
                session["job"] = {
                    "status": "running",
                    "stage": "准备分析",
                    "progress": 0.0,
                    "message": "任务已启动。",
                    "error": "",
                }
                try:
                    session = self.patch(
                        session_id,
                        lambda fresh: {**fresh, "job": session["job"]},
                    )
                except BaseException:
                    _ACTIVE_ANALYSES.pop(session_id, None)
                    raise
        try:
            thread = threading.Thread(
                target=self._run_analysis,
                args=(session_id, token, speaker_count, transcribe, model),
                daemon=True,
                name=f"media-{session_id}",
            )
            thread.start()
        except BaseException:
            with _ANALYSIS_LOCK:
                if _ACTIVE_ANALYSES.get(session_id) == token:
                    _ACTIVE_ANALYSES.pop(session_id, None)
            raise
        return session

    def _run_analysis(
        self,
        session_id: str,
        token: str,
        speaker_count: int | None,
        transcribe: bool,
        model: str,
    ) -> None:
        """Run one analysis and always release the process-wide session slot."""
        try:
            self._analysis_worker(session_id, speaker_count, transcribe, model)
        finally:
            with _ANALYSIS_LOCK:
                if _ACTIVE_ANALYSES.get(session_id) == token:
                    _ACTIVE_ANALYSES.pop(session_id, None)

    def _analysis_worker(
        self,
        session_id: str,
        speaker_count: int | None,
        transcribe: bool,
        model: str,
    ) -> None:
        last = {"progress": -1.0, "stage": ""}

        def progress(fraction: float, stage: str) -> None:
            # A long movie produces thousands of embedding windows. Persisting
            # every one would turn progress reporting into the bottleneck, so
            # only write when the bar moves by 2% or the stage changes.
            if stage == last["stage"] and float(fraction) - last["progress"] < 0.02 and float(fraction) < 1.0:
                return
            last["progress"] = float(fraction)
            last["stage"] = stage

            def patch_job(fresh: dict[str, Any]) -> dict[str, Any] | None:
                job = fresh.get("job") or {}
                if job.get("status") != "running":
                    return None
                fresh["job"] = {
                    **job,
                    "progress": round(float(fraction), 4),
                    "stage": stage,
                    "message": f"{stage} {int(round(fraction * 100))}%",
                }
                return fresh

            self.patch(session_id, patch_job)

        try:
            self.analyze(
                session_id,
                speaker_count=speaker_count,
                transcribe=transcribe,
                model=model,
                progress=progress,
            )
        except Exception as exc:
            # Bind the message before the closure: Python unbinds ``exc`` when the
            # handler exits, and a lambda that reads it later would raise.
            error_text = str(exc)
            self.patch(
                session_id,
                lambda fresh: {
                    **fresh,
                    "job": {
                        "status": "fail",
                        "stage": "失败",
                        "progress": 0.0,
                        "message": "分析失败。",
                        "error": error_text,
                    },
                },
            )

    # --------------------------------------------------------------- select ---

    def select_speaker(self, session_id: str, speaker_id: str) -> dict[str, Any]:
        session = self.load(session_id)
        available = {row.get("speaker_id") for row in session.get("speakers") or []}
        if speaker_id not in available:
            raise MediaIngestError("指定的说话人不存在。")
        return self.patch(
            session_id,
            lambda fresh: {**fresh, "selected_speaker": speaker_id, "dataset": None},
        )

    # --------------------------------------------------------------- export ---

    def export_dataset(
        self,
        session_id: str,
        *,
        speaker_id: str | None = None,
        include_failed: bool = False,
        require_text: bool = True,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Build a dataset from one analysis version.

        ``run_id`` selects a historical run from ``session["runs"]``; without it
        the active run is used. The run's own ``segments.json`` is authoritative,
        so exporting an older run is not affected by a later re-analysis.
        """
        session = self.load(session_id)
        if not session.get("rights", {}).get("declared"):
            raise MediaIngestError("该会话没有权利声明，不能导出训练集。")
        target = speaker_id or session.get("selected_speaker") or ""
        if not target:
            raise MediaIngestError("请先选择一个目标说话人。")
        run, segments = self._resolve_run_segments(session, run_id)
        available = {
            str(row.get("speaker_id") or "")
            for row in session.get("speakers") or []
            if isinstance(row, dict)
        }
        # A historical run may reference a speaker the current run no longer has.
        available.update(
            str(segment.get("speaker_id") or "")
            for segment in segments
            if isinstance(segment, dict)
        )
        if target not in available:
            raise MediaIngestError("目标说话人不存在。")
        run_id_value = str((run or {}).get("run_id") or "")

        accepted, rejected = self._select_export_segments(
            segments, target, include_failed=include_failed, require_text=require_text
        )
        if not accepted:
            raise MediaIngestError("没有可导出的片段；请先转写、处理质检失败的片段，或勾选允许失败片段。")

        accepted.sort(key=lambda row: float(row.get("start") or 0.0))
        dataset_id = f"ds_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{target}_{uuid.uuid4().hex[:4]}"
        dataset_dir = self.session_dir(session_id) / "dataset" / dataset_id
        dataset_dir.mkdir(parents=True, exist_ok=False)

        val_count = max(1, int(round(len(accepted) * 0.1))) if len(accepted) >= 5 else 0
        val = accepted[-val_count:] if val_count else []
        train = accepted[:-val_count] if val_count else accepted
        language = _list_language(session.get("language", "zh"))

        list_lines: list[str] = []
        manifest_segments: list[dict[str, Any]] = []
        for segment in accepted:
            path = self._absolute(session_id, segment["path"])
            text = str(segment.get("text") or "").replace("|", "｜")
            list_lines.append(f"{path}|sliced|{language}|{text}")
            manifest_segments.append(
                {
                    "segment_id": segment["segment_id"],
                    "speaker_id": segment["speaker_id"],
                    "start": segment["start"],
                    "end": segment["end"],
                    "duration_seconds": segment["duration_seconds"],
                    "audio": segment["path"],
                    "sha256": segment["audio_sha256"],
                    "text": text,
                    "qc_status": (segment.get("qc") or {}).get("status", "unknown"),
                }
            )
        list_path = dataset_dir / "dataset.list"
        list_path.write_text("\n".join(list_lines) + "\n", encoding="utf-8")
        checksums = []
        for segment in accepted:
            path = self._absolute(session_id, segment["path"])
            checksums.append(f"{segment['audio_sha256']}  {path.name}")
        (dataset_dir / "checksums.sha256").write_text("\n".join(checksums) + "\n", encoding="utf-8")

        speaker = next((row for row in session.get("speakers") or [] if row.get("speaker_id") == target), {})
        manifest = {
            "schema_version": 1,
            "dataset_id": dataset_id,
            "session_id": session_id,
            "run_id": run_id_value,
            "speaker_id": target,
            "speaker": speaker,
            "created_at": _now(),
            "language": session.get("language", "zh"),
            "source": session.get("source"),
            "rights": session.get("rights"),
            "stats": {
                "segment_count": len(accepted),
                "duration_seconds": round(sum(float(row.get("duration_seconds") or 0.0) for row in accepted), 3),
                "train": len(train),
                "val": len(val),
                "rejected": len(rejected),
            },
            "split": {
                "train": [row["segment_id"] for row in train],
                "val": [row["segment_id"] for row in val],
            },
            "rejected": rejected,
            "segments": manifest_segments,
            "train_ready": all(str(row.get("text") or "").strip() for row in accepted),
        }
        manifest_path = dataset_dir / "manifest.json"
        _atomic_json(manifest_path, manifest)

        dataset = {
            "dataset_id": dataset_id,
            "run_id": run_id_value,
            "directory": self._relative(session_id, dataset_dir),
            "list": self._relative(session_id, list_path),
            "manifest": self._relative(session_id, manifest_path),
            "checksums": self._relative(session_id, dataset_dir / "checksums.sha256"),
            "stats": manifest["stats"],
            "train_ready": manifest["train_ready"],
        }
        session["dataset"] = dataset
        self.patch(session_id, lambda fresh: {**fresh, "dataset": dataset})
        return dataset

    def _select_export_segments(
        self,
        segments: list[dict[str, Any]],
        target: str,
        *,
        include_failed: bool,
        require_text: bool,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        seen_hashes: set[str] = set()
        for segment in segments:
            if segment.get("speaker_id") != target:
                continue
            reason = ""
            if not include_failed and (segment.get("qc") or {}).get("status") == "fail":
                reason = "质检未通过"
            elif segment.get("audio_sha256") in seen_hashes:
                reason = "重复片段"
            elif require_text and not str(segment.get("text") or "").strip():
                reason = "缺少转写文本"
            if reason:
                rejected.append({"segment_id": segment.get("segment_id"), "reason": reason})
                continue
            seen_hashes.add(segment.get("audio_sha256") or "")
            accepted.append(segment)
        return accepted, rejected

    def activate_dataset(
        self,
        session_id: str,
        *,
        speaker_id: str | None = None,
        include_failed: bool = False,
        require_text: bool = True,
        dataset_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Copy slices into the training workspace.

        Two modes:

        * ``dataset_id`` given: replay a dataset exactly as its ``manifest.json``
          recorded it. A training set exported months ago can be re-activated
          unchanged even after the session was re-analyzed.
        * otherwise: compute the set from the active run (or ``run_id``) with the
          same accepted/rejected rules as :meth:`export_dataset`.

        The vendored preparation scripts find audio by basename inside
        ``custom_data/sliced``. This method performs the explicit hand-off: copy
        slices, write ``sliced_corrected.list`` and back the previous list up first.
        """
        session = self.load(session_id)
        if not session.get("rights", {}).get("declared"):
            raise MediaIngestError("该会话没有权利声明，不能写入训练工作区。")
        if dataset_id:
            return self._activate_saved_dataset(session, dataset_id, speaker_id=speaker_id, run_id=run_id)

        target = speaker_id or session.get("selected_speaker") or ""
        if not target:
            raise MediaIngestError("请先选择一个目标说话人。")
        run, segments = self._resolve_run_segments(session, run_id)
        available = {
            str(row.get("speaker_id") or "")
            for row in session.get("speakers") or []
            if isinstance(row, dict)
        }
        available.update(
            str(segment.get("speaker_id") or "")
            for segment in segments
            if isinstance(segment, dict)
        )
        if target not in available:
            raise MediaIngestError("目标说话人不存在。")
        accepted, rejected = self._select_export_segments(
            segments, target, include_failed=include_failed, require_text=require_text
        )
        if not accepted:
            raise MediaIngestError("没有可写入训练工作区的片段。")
        accepted.sort(key=lambda row: float(row.get("start") or 0.0))
        return self._write_training_workspace(
            session,
            [
                {
                    "segment_id": str(segment["segment_id"]),
                    "path": str(segment["path"]),
                    "text": str(segment.get("text") or ""),
                }
                for segment in accepted
            ],
            speaker_id=target,
            language=str(session.get("language") or "zh"),
            rejected=len(rejected),
            dataset_id="",
            run_id=str((run or {}).get("run_id") or ""),
        )

    def _activate_saved_dataset(
        self,
        session: dict[str, Any],
        dataset_id: str,
        *,
        speaker_id: str | None,
        run_id: str | None,
    ) -> dict[str, Any]:
        """Replay an already exported dataset from its frozen manifest."""
        if not DATASET_ID_PATTERN.fullmatch(str(dataset_id or "")):
            raise MediaIngestError("非法的数据集 ID。")
        session_id = str(session["session_id"])
        dataset_dir = self._absolute(session_id, f"dataset/{dataset_id}")
        manifest_path = dataset_dir / "manifest.json"
        if not dataset_dir.is_dir() or not manifest_path.is_file():
            raise MediaIngestError("数据集不存在。")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MediaIngestError("数据集清单损坏。") from exc
        if not isinstance(manifest, dict):
            raise MediaIngestError("数据集清单损坏。")
        if str(manifest.get("dataset_id") or dataset_id) != dataset_id:
            raise MediaIngestError("数据集清单 ID 不匹配。")
        if str(manifest.get("session_id") or session_id) != session_id:
            raise MediaIngestError("数据集不属于这个采集会话。")
        manifest_run = str(manifest.get("run_id") or "")
        if run_id and manifest_run and str(run_id) != manifest_run:
            raise MediaIngestError("指定的分析版本与数据集清单不一致。")
        target = str(manifest.get("speaker_id") or speaker_id or "")
        if speaker_id and target and str(speaker_id) != target:
            raise MediaIngestError("指定的说话人与数据集清单不一致。")
        if not target:
            raise MediaIngestError("数据集清单缺少说话人。")

        rows: list[dict[str, Any]] = []
        for index, segment in enumerate(manifest.get("segments") or [], start=1):
            if not isinstance(segment, dict):
                continue
            relative = str(segment.get("audio") or segment.get("path") or "")
            if not relative:
                raise MediaIngestError("数据集清单缺少切片路径。")
            rows.append(
                {
                    "segment_id": str(segment.get("segment_id") or f"seg_{index:04d}"),
                    "path": relative,
                    "text": str(segment.get("text") or ""),
                }
            )
        if not rows:
            raise MediaIngestError("数据集清单里没有可写入的切片。")
        rejected_rows = manifest.get("rejected")
        rejected = len(rejected_rows) if isinstance(rejected_rows, list) else 0

        result = self._write_training_workspace(
            session,
            rows,
            speaker_id=target,
            language=str(manifest.get("language") or session.get("language") or "zh"),
            rejected=rejected,
            dataset_id=dataset_id,
            run_id=manifest_run,
        )
        # The HTTP layer builds download URLs from session["dataset"], so point it
        # at the dataset that was just activated.
        session["dataset"] = {
            "dataset_id": dataset_id,
            "run_id": manifest_run,
            "directory": f"dataset/{dataset_id}",
            "list": f"dataset/{dataset_id}/dataset.list",
            "manifest": f"dataset/{dataset_id}/manifest.json",
            "checksums": f"dataset/{dataset_id}/checksums.sha256",
            "stats": manifest.get("stats") or {},
            "train_ready": bool(manifest.get("train_ready")),
        }
        self.patch(session_id, lambda fresh: {**fresh, "dataset": session["dataset"]})
        return result

    def _write_training_workspace(
        self,
        session: dict[str, Any],
        rows: list[dict[str, Any]],
        *,
        speaker_id: str,
        language: str,
        rejected: int,
        dataset_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        """Copy ``rows`` and rewrite the training lists, backing the old ones up."""
        session_id = str(session["session_id"])
        sliced_dir = self.root / "custom_data" / "sliced"
        asr_dir = self.root / "custom_data" / "asr"
        sliced_dir.mkdir(parents=True, exist_ok=True)
        asr_dir.mkdir(parents=True, exist_ok=True)
        stamp = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}-{uuid.uuid4().hex[:4]}"
        list_path = asr_dir / "sliced_corrected.list"
        whisper_path = asr_dir / "sliced_whisper.list"
        backup = ""
        whisper_backup = ""
        if list_path.is_file():
            backup_path = asr_dir / f"sliced_corrected.list.bak-{stamp}"
            shutil.copy2(list_path, backup_path)
            backup = str(backup_path)
        if whisper_path.is_file():
            whisper_backup_path = asr_dir / f"sliced_whisper.list.bak-{stamp}"
            shutil.copy2(whisper_path, whisper_backup_path)
            whisper_backup = str(whisper_backup_path)

        list_language = _list_language(language)
        lines: list[str] = []
        for row in rows:
            segment_id = str(row["segment_id"])
            # The destination name is derived from it, so a manifest edited on
            # disk must not be able to walk out of custom_data/sliced.
            if not SEGMENT_ID_PATTERN.fullmatch(segment_id):
                raise MediaIngestError(f"非法的切片 ID：{segment_id}")
            source = self._absolute(session_id, str(row["path"]))
            if not source.is_file():
                raise MediaIngestError(f"切片文件不存在，无法写入训练工作区：{segment_id}")
            destination = sliced_dir / f"media_{session_id}_{segment_id}.wav"
            shutil.copy2(source, destination)
            text = str(row.get("text") or "").replace("|", "｜")
            lines.append(f"{destination}|sliced|{list_language}|{text}")
        payload = "\n".join(lines) + "\n"
        # Both lists are read by a running feature-extraction/training subprocess,
        # so they are replaced atomically: a crash mid-write must never leave a
        # truncated list that the trainer would silently train on.
        atomic_write_text(list_path, payload)
        text_ready = all(str(row.get("text") or "").strip() for row in rows)
        if text_ready:
            atomic_write_text(whisper_path, payload)
        return {
            "copied": len(rows),
            "rejected": int(rejected),
            "list": str(list_path),
            "whisper_list": str(whisper_path) if text_ready else "",
            "backup": backup,
            "whisper_backup": whisper_backup,
            "text_ready": text_ready,
            "speaker_id": speaker_id,
            "dataset_id": dataset_id,
            "run_id": run_id,
        }

    # -------------------------------------------------------------- helpers ---

    def read_source_audio(self, session_id: str) -> tuple[np.ndarray, int]:
        session = self.load(session_id)
        source = session.get("source") or {}
        if not source.get("export_audio"):
            raise MediaIngestError("还没有上传媒体。")
        audio, rate = sf.read(str(self._absolute(session_id, source["export_audio"])), dtype="float32", always_2d=True)
        mono = audio.mean(axis=1) if audio.shape[1] > 1 else audio[:, 0]
        return np.ascontiguousarray(mono, dtype=np.float32), int(rate)


def _list_language(language: str) -> str:
    return {"zh": "ZH", "yue": "ZH", "en": "EN", "ja": "JA", "ko": "KO"}.get(language, "ZH")
