"""Media analysis pipeline: diarization -> slices -> transcripts -> datasets.

This module composes the storage layer (:class:`backend.services.media_sessions.MediaSessionStore`)
with the compute side of media intake — the ffmpeg-free analysis steps that read
the prepared 16/32 kHz tracks, run VAD + ERes2NetV2 speaker clustering, cut
2–10 second slices, QC them against the reference-audio gate, optionally
transcribe with faster-whisper, and export/activate training datasets. Nothing
here reaches into the GPT-SoVITS training pipeline; export produces a standard
``dataset.list`` plus a manifest that can be consumed later.
"""

from __future__ import annotations

import json
import shutil
import threading
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from backend.infra.atomic import atomic_write_text
from backend.infra.gpu import GPU_ARBITER
from backend.services import media_diarization, reference_audio, whisper_cache
from backend.services.media_sessions import (
    _PROGRESS,
    DATASET_ID_PATTERN,
    SEGMENT_ID_PATTERN,
    MediaIngestError,
    MediaSessionStore,
    _atomic_json,
    _new_run_id,
    _now,
    _sha256_file,
)

# ``_media_service()`` in app.py builds a fresh MediaIngestService for every
# request, so the per-instance lock alone cannot stop two concurrent analyze
# requests from both starting a worker (and loading ERes2NetV2 twice). This
# registry is process-wide on purpose: check, mark job=running, and register must
# happen under one lock, and the entry must be removed when the worker ends.
_ACTIVE_ANALYSES: dict[str, str] = {}
_ANALYSIS_LOCK = threading.Lock()


def _load_whisper(model_name: str):
    """Patchable seam; the model itself is cached process-wide in ``whisper_cache``.

    A private ``lru_cache`` here duplicated the large-v3 model whenever reference
    preparation had already loaded it (see ``whisper_cache`` module docstring).
    """
    return whisper_cache.load_whisper(model_name)


def _list_language(language: str) -> str:
    return {"zh": "ZH", "yue": "ZH", "en": "EN", "ja": "JA", "ko": "KO"}.get(language, "ZH")


class MediaAnalysisMixin:
    """The compute half of ``MediaIngestService``; storage lives in ``MediaSessionStore``."""

    # -------------------------------------------------------------- diarize ---

    def _embed(self, audio: np.ndarray, rate: int) -> np.ndarray:
        if self._embedder is None:
            with self._embed_lock:
                if self._embedder is None:
                    from scripts.experiments.speaker_embedding import SpeakerEmbedder

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


class MediaIngestService(MediaAnalysisMixin, MediaSessionStore):
    """Owns media sessions and their derived artifacts."""

    # Storage and upload live in ``MediaSessionStore``; the analysis pipeline and
    # dataset export/activation live in ``MediaAnalysisMixin``.
