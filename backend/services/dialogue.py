"""Dialogue scripts: parse ``名字：台词`` text and synthesise per-speaker lines.

A dialogue job walks a parsed script line by line, overlaying the bound voice
card (``voice_assets.apply_voice_card``) for each role before handing the single
line to ``core.synthesize_lines`` (which owns the GPU lease). One broken line —
including a missing voice card — is recorded as a failure and never aborts the
rest of the episode.
"""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from backend.services import core, voice_assets

DEFAULT_SPEAKER = "旁白"
TERMINAL_STATUSES = {"done", "failed"}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload)
    if isinstance(data.get("outputs"), list):
        data["outputs"] = [dict(item) for item in data["outputs"]]
    return data


def parse_dialogue_script(text: str) -> list[dict[str, str]]:
    """Parse a short-drama script into ``[{"speaker": ..., "line": ...}]``.

    Deliberately simple rules:

    - Blank lines and lines starting with ``#`` (comments) are skipped.
    - A line splits at its FIRST full-width ``：`` or half-width ``:`` colon. The
      speaker is the entire run of text immediately before that colon, kept
      verbatim — inner spaces are NOT removed (``小明 说：你好`` yields speaker
      ``小明 说``); only surrounding whitespace is stripped.
    - A line without any colon belongs to ``DEFAULT_SPEAKER`` (旁白).
    - Entries whose line part is empty after stripping are skipped.
    """
    parsed: list[dict[str, str]] = []
    for raw in (text or "").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "：" in stripped:
            speaker, line = stripped.split("：", 1)
        elif ":" in stripped:
            speaker, line = stripped.split(":", 1)
        else:
            speaker, line = DEFAULT_SPEAKER, stripped
        speaker = speaker.strip()
        line = line.strip()
        if not line:
            continue
        parsed.append({"speaker": speaker or DEFAULT_SPEAKER, "line": line})
    return parsed


@dataclass
class _DialogueJob:
    job_id: str
    snapshot: dict[str, Any]
    revision: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock)


class DialogueJobManager:
    """Background dialogue synthesis jobs with polling-friendly snapshots."""

    def __init__(self, card_lookup: Callable[[str], dict[str, Any]] | None = None) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, _DialogueJob] = {}
        self._card_lookup: Callable[[str], dict[str, Any]] = (
            card_lookup if card_lookup is not None else voice_assets.VoiceCardStore().get
        )

    def start(
        self,
        script_text: str,
        role_bindings: dict[str, str],
        base_config: dict[str, Any],
        output_dir: Path | None = None,
    ) -> dict[str, Any]:
        parsed = parse_dialogue_script(script_text)
        if not parsed:
            raise ValueError("对话脚本为空，请先输入内容。")
        # One episode owns ONE output directory. Previously every line called
        # ``core.synthesize_lines(output_dir=None)`` and each got its own
        # timestamped folder, so the episode was scattered across N directories
        # and the export fell back to the process working directory.
        destination = (
            Path(output_dir) if output_dir is not None else core.new_output_dir()
        )
        job_id = f"dlg_{int(time.time() * 1000)}"
        job = _DialogueJob(
            job_id=job_id,
            snapshot={
                "job_id": job_id,
                "status": "running",
                "current": 0,
                "total": len(parsed),
                "completed": 0,
                "outputs": [],
                "output_dir": str(destination),
                "error": "",
                "message": "对话合成任务已启动。",
                "started_at": _now(),
                "updated_at": _now(),
                "revision": 0,
            },
        )
        with self._lock:
            self._jobs[job_id] = job

        bindings = dict(role_bindings or {})
        base = dict(base_config or {})

        def _run() -> None:
            records: list[dict[str, Any]] = []
            try:
                for index, entry in enumerate(parsed, 1):
                    speaker, line = entry["speaker"], entry["line"]
                    voice_card_id = str(bindings.get(speaker) or "")
                    records.append(
                        self._synthesize_line(base, speaker, line, voice_card_id, destination)
                    )
                    self.update(
                        job_id,
                        {
                            "status": "running",
                            "current": index,
                            "total": len(parsed),
                            "completed": len([item for item in records if item["file"]]),
                            "outputs": [dict(item) for item in records],
                            "message": f"对话合成进行中 {index}/{len(parsed)}。",
                        },
                    )
                completed = len([item for item in records if item["file"]])
                broken = len(records) - completed
                message = f"对话合成完成 {completed}/{len(records)}。"
                if broken:
                    message = f"对话合成完成 {completed}/{len(records)}，其中 {broken} 句失败。"
                self.update(
                    job_id,
                    {
                        "status": "done",
                        "current": len(parsed),
                        "total": len(parsed),
                        "completed": completed,
                        "outputs": [dict(item) for item in records],
                        "error": "",
                        "message": message,
                        "ended_at": _now(),
                    },
                )
            except Exception as exc:  # defensive: per-line errors are captured above
                self.update(
                    job_id,
                    {
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                        "message": "对话合成任务失败。",
                        "ended_at": _now(),
                    },
                )

        threading.Thread(target=_run, daemon=True, name=job_id).start()
        return self.snapshot(job_id) or {}

    def _synthesize_line(
        self,
        base_config: dict[str, Any],
        speaker: str,
        line: str,
        voice_card_id: str,
        destination: Path | None,
    ) -> dict[str, Any]:
        try:
            cfg = dict(base_config)
            if voice_card_id:
                card = self._card_lookup(voice_card_id)
                cfg = voice_assets.apply_voice_card(cfg, card)
            result = core.synthesize_lines(cfg, line, output_dir=destination)
            outputs = result.get("outputs") or []
            output = outputs[0] if outputs else {}
            file_value = str(output.get("file") or "")
            if file_value:
                try:
                    duration = max(0.0, float(output.get("duration") or 0.0))
                except (TypeError, ValueError):
                    duration = 0.0
                return {
                    "speaker": speaker,
                    "line": line,
                    "text": str(output.get("text") or line),
                    "rendered_text": str(output.get("rendered_text") or ""),
                    "file": file_value,
                    "duration": duration,
                    "error": "",
                    "voice_card_id": voice_card_id,
                    "role_voice": voice_card_id,
                }
            return {
                "speaker": speaker,
                "line": line,
                "file": "",
                "error": str(output.get("error") or "该句未产出音频文件。"),
                "voice_card_id": voice_card_id,
                "role_voice": voice_card_id,
            }
        except Exception as exc:
            # A missing card or a synthesis blow-up fails this line only.
            return {
                "speaker": speaker,
                "line": line,
                "file": "",
                "error": f"{type(exc).__name__}: {exc}",
                "voice_card_id": voice_card_id,
                "role_voice": voice_card_id,
            }

    def snapshot(self, dialogue_job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(dialogue_job_id)
        if not job:
            return None
        with job.lock:
            return _snapshot(job.snapshot)

    def update(self, dialogue_job_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        job = self._jobs.get(dialogue_job_id)
        if not job:
            return None
        with job.lock:
            job.revision += 1
            job.snapshot.update(payload)
            job.snapshot["job_id"] = dialogue_job_id
            job.snapshot["revision"] = job.revision
            job.snapshot["updated_at"] = _now()
            return _snapshot(job.snapshot)

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """Recent job snapshots, newest first, as deep copies."""
        try:
            count = max(0, int(limit))
        except (TypeError, ValueError):
            count = 20
        if count == 0:
            return []
        with self._lock:
            jobs = list(self._jobs.values())[-count:]
        snapshots: list[dict[str, Any]] = []
        for job in reversed(jobs):
            with job.lock:
                snapshots.append(copy.deepcopy(job.snapshot))
        return snapshots


DIALOGUE_JOBS = DialogueJobManager()
