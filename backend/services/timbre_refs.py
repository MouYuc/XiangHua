"""Reference corpus and conversion-source management for timbre conversion.

kNN-VC wants minutes of speech rather than the single profile clip the
synthesis engine needs, so this module decides *which* recordings define a
voice (the GPT-SoVITS slice corpus first, the profile clip as fallback), when a
cached matching pool is still valid (``corpus_digest`` hashes names, sizes and
mtimes), and where an uploaded conversion source may land (isolated from the
reference corpus so a converted song cannot drift into the user's timbre).

The profile-pool building itself lives in ``timbre_features``; model lifecycle
and worker orchestration in ``timbre_worker``; ``timbre.py`` keeps the facade.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from backend.infra.paths import CONFIG_DIR, REPO_ROOT
from backend.services import core

TIMBRE_DIR = CONFIG_DIR / "timbre"
PROFILE_CACHE_DIR = TIMBRE_DIR / "matching_sets"
OUTPUT_ROOT = REPO_ROOT / "output" / "vc" / "webui"

AUDIO_SUFFIXES = {".wav", ".flac", ".mp3", ".m4a", ".ogg"}


def reference_files(profile_id: str, config: dict[str, Any]) -> list[Path]:
    """List the reference recordings that define a voice profile.

    A ``VoiceProfile`` carries one ``ref_audio`` clip, which is what the
    synthesis engine needs, but kNN-VC wants minutes of speech. The whole
    GPT-SoVITS slice corpus is therefore preferred when it exists, because that
    *is* the user's voice, and the single profile clip is the fallback.
    """
    slices_dir = Path(str(config.get("gpt_sovits_root") or "")) / "custom_data" / "sliced"
    found: list[Path] = []
    if slices_dir.is_dir():
        found = sorted(
            path for path in slices_dir.iterdir()
            if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES
        )
    if found:
        return found

    fallback = Path(str(config.get("ref_audio") or ""))
    if fallback.is_file():
        return [fallback]
    return []


def corpus_digest(paths: list[Path]) -> str:
    """Hash the identity *and* the state of the reference corpus.

    Sizes and mtimes are included so that re-recording over an existing filename
    rebuilds the matching pool. A digest over names alone would keep serving
    frames of the previous take.
    """
    hasher = hashlib.sha256()
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        hasher.update(str(path.resolve()).encode("utf-8"))
        hasher.update(str(stat.st_size).encode("utf-8"))
        hasher.update(str(int(stat.st_mtime)).encode("utf-8"))
    return hasher.hexdigest()[:16]


def source_dir() -> Path:
    """Where uploaded conversion sources are stored, isolated from references."""
    path = OUTPUT_ROOT / "sources"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_source(filename: str, stream: Any) -> Path:
    """Persist an uploaded source recording under a generated, safe name.

    Uploads land in their own directory rather than alongside the voice-profile
    reference audio: mixing them would let a converted song drift into the kNN
    matching pool and quietly change the user's timbre.
    """
    import shutil

    name = Path(filename or "source.wav").name
    suffix = Path(name).suffix.lower()
    if suffix not in AUDIO_SUFFIXES:
        raise ValueError(f"不支持的音频格式：{suffix or '（无扩展名）'}")
    destination = source_dir() / f"{int(time.time())}_{name}"
    with destination.open("wb") as output:
        shutil.copyfileobj(stream, output)
    return destination


def disambiguate_labels(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make repeated file names tell themselves apart in the picker.

    Synthesis runs write into per-experiment directories, so the same file name
    can legitimately appear several times with different audio behind it. Two rows
    reading ``001_你好.mp3`` are indistinguishable to the user, so a colliding name
    is qualified with its parent directory, which is the experiment name.
    """
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["label"]] = counts.get(row["label"], 0) + 1
    for row in rows:
        if counts.get(row["label"], 0) > 1:
            parent = Path(str(row["path"])).parent.name
            if parent:
                row["label"] = f"{parent}/{row['label']}"
    return rows


def list_sources(limit: int = 30) -> list[dict[str, Any]]:
    """List audio the user can convert: synthesis history plus past uploads."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    try:
        for entry in core.load_history_with_downloads():
            path = Path(str(entry.get("file") or ""))
            if not path.is_file():
                continue
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "label": path.name,
                    "path": str(path),
                    "download_id": str(entry.get("download_id") or ""),
                    "origin": "synthesis",
                    "created_at": str(entry.get("time") or ""),
                    "bytes": path.stat().st_size,
                }
            )
    except Exception:
        pass

    for path in sorted(source_dir().iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not path.is_file() or path.suffix.lower() not in AUDIO_SUFFIXES:
            continue
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "label": path.name,
                "path": str(path),
                "download_id": "",
                "origin": "upload",
                "created_at": time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime)
                ),
                "bytes": path.stat().st_size,
            }
        )
    return disambiguate_labels(rows[:limit])
