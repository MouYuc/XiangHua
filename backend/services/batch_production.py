"""Whole-episode delivery exports: SRT subtitles, the full track, and stems.

Turns one synthesis run's per-line outputs (``core.synthesize_lines`` shape:
``file`` / ``duration`` / ``text`` per output) into the artifacts a short-drama
episode needs: an ``episode.srt`` on a cumulative timeline, one concatenated
``episode_full.<suffix>`` track, and — when speaker roles are supplied — one
concatenated stem per role under ``stems/``.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import soundfile as sf

from tools.rules import safe_name


class BatchExportError(ValueError):
    """User-readable export failure; messages are Chinese."""


def srt_timestamp(seconds: float) -> str:
    """Format seconds as an SRT timestamp ``HH:MM:SS,mmm``; negatives clamp to zero."""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        value = 0.0
    total_ms = max(0, int(round(value * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds_part, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds_part:02d},{millis:03d}"


def build_srt_blocks(lines: list[dict[str, Any]], gap_seconds: float = 0.0) -> str:
    """Render SRT blocks on a cumulative timeline.

    Each entry carries ``text`` and ``duration``; an optional ``speaker`` prefixes
    the caption with ``【speaker】``. Entries whose text is blank are skipped so a
    caption line is never empty. ``gap_seconds`` of silence is inserted after
    every entry; invalid or negative values fall back to 0.
    """
    blocks: list[str] = []
    cursor = 0.0
    try:
        gap = max(0.0, float(gap_seconds))
    except (TypeError, ValueError):
        gap = 0.0
    for item in lines:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        speaker = str(item.get("speaker") or "").strip()
        try:
            duration = max(0.0, float(item.get("duration") or 0.0))
        except (TypeError, ValueError):
            duration = 0.0
        start, end = cursor, cursor + duration
        caption = f"【{speaker}】{text}" if speaker else text
        blocks.append(
            f"{len(blocks) + 1}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{caption}"
        )
        cursor = end + gap
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def concat_audio(files: list[Path], destination: Path) -> Path:
    """Join audio files end-to-end into ``destination``.

    Uses ffmpeg's concat demuxer with stream copy. Without ffmpeg, a single
    input is plain-copied and multiple inputs are refused. A failed run never
    leaves a half-written destination behind.
    """
    paths = [Path(item) for item in files]
    if not paths:
        raise BatchExportError("没有可拼接的音频文件。")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        if len(paths) == 1:
            return Path(shutil.copy2(paths[0], destination))
        raise BatchExportError("没有 ffmpeg，无法拼接多段音频。")
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix="concat_", suffix=".txt", delete=False
    ) as handle:
        for path in paths:
            # The concat demuxer wants forward slashes inside single quotes;
            # an embedded quote is closed, escaped, and reopened.
            posix = str(path.resolve()).replace("\\", "/")
            handle.write(f"file '{posix.replace(chr(39), chr(39) + chr(92) + chr(39))}'\n")
        list_path = Path(handle.name)
    try:
        completed = subprocess.run(
            [
                ffmpeg, "-y", "-f", "concat", "-safe", "0",
                "-i", str(list_path), "-c", "copy", str(destination),
            ],
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            destination.unlink(missing_ok=True)
            detail = (completed.stderr or "").strip().splitlines()
            raise BatchExportError(
                f"ffmpeg 拼接音频失败：{detail[-1] if detail else '未知错误'}"
            )
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise BatchExportError(f"无法运行 ffmpeg：{exc}") from exc
    finally:
        list_path.unlink(missing_ok=True)
    return destination


def group_by_speaker(
    outputs: list[dict[str, Any]], speakers: dict[int, str]
) -> dict[str, list[Path]]:
    """Bucket output files by speaker name.

    ``speakers`` maps ``output_index -> 角色名``; outputs without a role or
    without a file value are skipped.
    """
    groups: dict[str, list[Path]] = {}
    for index, item in enumerate(outputs):
        speaker = str(speakers.get(index) or "").strip()
        if not speaker:
            continue
        file_value = str(item.get("file") or "")
        if not file_value:
            continue
        groups.setdefault(speaker, []).append(Path(file_value))
    return groups


def _line_duration(item: dict[str, Any], path: Path) -> float:
    """Prefer the output's own ``duration``; otherwise read the file, else 0."""
    raw = item.get("duration")
    if raw is not None:
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            pass
    try:
        return max(0.0, float(sf.info(str(path)).duration))
    except (OSError, RuntimeError):
        return 0.0


def export_episode(
    outputs: list[dict[str, Any]],
    output_dir: Path,
    speakers: dict[int, str] | None = None,
) -> dict[str, Any]:
    """Export one episode package from core synthesis outputs.

    A missing audio file only skips that line (counted in ``failed``); it never
    aborts the export. Writes ``episode.srt`` and ``episode_full.<suffix>``
    (suffix of the first usable file) into ``output_dir``; with ``speakers`` it
    also writes ``stems/<角色名>.<suffix>`` per role, the role name cleaned by
    ``safe_name`` (a cleaning collision gets a numeric suffix).
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    speakers = speakers or {}

    usable: list[tuple[int, dict[str, Any], Path]] = []
    failed = 0
    for index, item in enumerate(outputs):
        file_value = str(item.get("file") or "")
        path = Path(file_value) if file_value else None
        if path is None or not path.is_file():
            failed += 1
            continue
        usable.append((index, item, path))

    srt_lines = [
        {
            "text": str(item.get("text") or ""),
            "duration": _line_duration(item, path),
            "speaker": str(speakers.get(index) or ""),
        }
        for index, item, path in usable
    ]
    srt_path = output_dir / "episode.srt"
    srt_path.write_text(build_srt_blocks(srt_lines), encoding="utf-8")

    full = ""
    if usable:
        suffix = usable[0][2].suffix or ".wav"
        full_path = output_dir / f"episode_full{suffix}"
        concat_audio([path for _, _, path in usable], full_path)
        full = str(full_path)

    stems: dict[str, str] = {}
    used_stem_names: set[str] = set()
    for speaker, paths in group_by_speaker(outputs, speakers).items():
        existing = [path for path in paths if path.is_file()]
        if not existing:
            continue
        base = safe_name(speaker)
        name = base
        counter = 2
        while name in used_stem_names:
            name = f"{base}_{counter}"
            counter += 1
        used_stem_names.add(name)
        stem_path = output_dir / "stems" / f"{name}{existing[0].suffix}"
        concat_audio(existing, stem_path)
        stems[speaker] = str(stem_path)

    return {
        "srt": str(srt_path),
        "full": full,
        "stems": stems,
        "line_count": len(usable),
        "failed": failed,
    }
