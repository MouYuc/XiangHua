"""Media session storage and upload preparation.

The service keeps one JSON session per upload under
``<gpt_sovits_root>/custom_data/media/<session_id>/session.json``. Audio and
derived slices live next to it. This module owns the durable side of media
intake — session files, locks, upload validation, and the ffmpeg probing that
turns an uploaded file into the 16 kHz analysis track and the 32 kHz export
track. The analysis pipeline (VAD, speaker clustering, slicing, transcription,
dataset export) lives in :mod:`backend.services.media_analysis`, which composes
its mixin with :class:`MediaSessionStore` to form the public
``MediaIngestService``.

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
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np
import soundfile as sf

from backend.infra.logging import get_logger

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


class MediaSessionStore:
    """Owns media sessions, their JSON files, uploads, and run versioning.

    The analysis pipeline in :mod:`backend.services.media_analysis` is mixed in
    on top of this class; together they form ``MediaIngestService``.
    """

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

    # -------------------------------------------------------------- helpers ---

    def read_source_audio(self, session_id: str) -> tuple[np.ndarray, int]:
        session = self.load(session_id)
        source = session.get("source") or {}
        if not source.get("export_audio"):
            raise MediaIngestError("还没有上传媒体。")
        audio, rate = sf.read(str(self._absolute(session_id, source["export_audio"])), dtype="float32", always_2d=True)
        mono = audio.mean(axis=1) if audio.shape[1] > 1 else audio[:, 0]
        return np.ascontiguousarray(mono, dtype=np.float32), int(rate)
