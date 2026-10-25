"""Reference-prompt lookup and pairing checks.

GPT-SoVITS conditions synthesis on a reference clip *and* its transcript. A
wrong transcript is not a cosmetic warning: it changes the prompt's content and
can make an otherwise correct voice sound unstable. This module keeps the
lookup deliberately small and local:

* only the three fixed ASR manifests are read from ``asr_dir``;
* the corrected manifest takes precedence over automatic transcripts;
* exact normalized paths are preferred, with a basename/timestamp fallback for
  the same slice copied to another drive or root;
* comparison ignores whitespace and punctuation and tolerates a small character
  error rate, which is the realistic noise in hand-maintained tags.

The service does not decide whether a caller-provided path is safe to read. It
only looks up the path against the fixed manifests.
"""

from __future__ import annotations

import posixpath
import unicodedata
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ASR_DIR = (
    Path(__file__).resolve().parents[2] / "GPT-SoVITS" / "custom_data" / "asr"
)

# Order is significant. A corrected transcript must win over an automatic one,
# and the automatic whisper transcript wins over the raw ASR list.
MANIFEST_FILES = (
    "sliced_corrected.list",
    "sliced_whisper.list",
    "sliced.list",
)

MAX_CER = 0.20


@dataclass(frozen=True)
class _Entry:
    path: str
    text: str
    source: str
    language: str
    path_key: str
    filename_key: str


def lookup_reference_prompt(
    audio_path: str | Path | None,
    asr_dir: str | Path | None = None,
) -> dict | None:
    """Return the manifest prompt for ``audio_path``, or ``None`` if unknown.

    Matching first normalizes path separators, ``.``/``..`` segments and case.
    If the full paths differ (for example, the audio was moved from ``G:\\`` to
    ``D:\\``), the filename is used as a fallback. Since slice filenames contain
    start/end timestamps, this still identifies a specific slice rather than
    merely some file in the same directory.
    """

    path_key = _path_key(audio_path)
    if not path_key:
        return None

    index = _load_index(asr_dir)
    entry = index["by_path"].get(path_key)
    if entry is None:
        entry = index["by_filename"].get(_filename_key(audio_path))
    if entry is None:
        return None

    return {
        "path": entry.path,
        "text": entry.text,
        "source": entry.source,
        "language": entry.language,
    }


def reference_pair_check(
    audio_path: str | Path | None,
    ref_text: str | None,
    asr_dir: str | Path | None = None,
) -> dict:
    """Compare caller-supplied reference text with the manifest prompt.

    ``status`` is one of:

    * ``matched``: normalized texts are equal or their CER is at most 0.2;
    * ``mismatched``: both texts exist but differ materially;
    * ``unknown``: the audio is absent from every fixed manifest;
    * ``empty``: the supplied or manifest text has no comparable characters.
    """

    actual_text = "" if ref_text is None else str(ref_text)
    prompt = lookup_reference_prompt(audio_path, asr_dir=asr_dir)
    if prompt is None:
        return {
            "status": "unknown",
            "expected_text": None,
            "actual_text": actual_text,
            "source": None,
            "message": "未在参考音清单中找到该音频。",
        }

    expected_text = prompt["text"]
    expected_normalized = _normalize_text(expected_text)
    actual_normalized = _normalize_text(actual_text)

    if not expected_normalized or not actual_normalized:
        message = (
            "参考文本为空，无法校验。"
            if not actual_normalized
            else "清单中的参考文本为空，无法校验。"
        )
        return {
            "status": "empty",
            "expected_text": expected_text,
            "actual_text": actual_text,
            "source": prompt["source"],
            "message": message,
            "cer": None,
        }

    cer = _character_error_rate(expected_normalized, actual_normalized)
    matched = cer <= MAX_CER
    if expected_normalized == actual_normalized:
        message = "参考文本与清单一致。"
    elif matched:
        message = f"参考文本与清单基本一致（CER {cer:.1%}）。"
    else:
        message = f"参考文本与清单不匹配（CER {cer:.1%}）。"

    return {
        "status": "matched" if matched else "mismatched",
        "expected_text": expected_text,
        "actual_text": actual_text,
        "source": prompt["source"],
        "message": message,
        "cer": cer,
    }


def _resolve_asr_dir(asr_dir: str | Path | None) -> Path:
    return Path(asr_dir) if asr_dir is not None else DEFAULT_ASR_DIR


def _load_index(asr_dir: str | Path | None) -> dict[str, dict[str, _Entry]]:
    """Load fixed manifests, keeping the first entry for duplicate keys."""

    root = _resolve_asr_dir(asr_dir)
    by_path: dict[str, _Entry] = {}
    by_filename: dict[str, _Entry] = {}

    for source in MANIFEST_FILES:
        manifest = root / source
        for entry in _read_manifest(manifest, source):
            by_path.setdefault(entry.path_key, entry)
            by_filename.setdefault(entry.filename_key, entry)

    return {"by_path": by_path, "by_filename": by_filename}


def _read_manifest(manifest: Path, source: str):
    try:
        handle = manifest.open("r", encoding="utf-8-sig", errors="replace")
    except OSError:
        return

    with handle:
        for raw_line in handle:
            line = raw_line.rstrip("\r\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue

            fields = line.split("|", 3)
            if len(fields) != 4:
                continue

            audio_path = fields[0].strip()
            language = fields[2].strip()
            text = fields[3].strip()
            path_key = _path_key(audio_path)
            filename_key = _filename_key(audio_path)
            if not path_key or not filename_key:
                continue

            yield _Entry(
                path=audio_path,
                text=text,
                source=source,
                language=language,
                path_key=path_key,
                filename_key=filename_key,
            )


def _path_key(value: str | Path | None) -> str:
    if value is None:
        return ""
    text = str(value).strip().replace("\\", "/")
    if not text:
        return ""
    return posixpath.normpath(text).casefold()


def _filename_key(value: str | Path | None) -> str:
    if value is None:
        return ""
    text = str(value).strip().replace("\\", "/").rstrip("/")
    if not text:
        return ""
    return posixpath.basename(text).casefold()


def _normalize_text(value: str | None) -> str:
    """Fold case/width, then drop whitespace and unicode punctuation."""

    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return "".join(
        char
        for char in normalized
        if not char.isspace() and unicodedata.category(char)[0] not in {"P", "Z", "C"}
    )


def _character_error_rate(expected: str, actual: str) -> float:
    """Levenshtein distance divided by the longer reference length."""

    if not expected and not actual:
        return 0.0
    if not expected or not actual:
        return 1.0

    # Keep the shorter string on the row axis to use less memory.
    if len(expected) < len(actual):
        expected, actual = actual, expected

    previous = list(range(len(actual) + 1))
    for row, expected_char in enumerate(expected, start=1):
        current = [row]
        for column, actual_char in enumerate(actual, start=1):
            insertion = current[column - 1] + 1
            deletion = previous[column] + 1
            substitution = previous[column - 1] + (expected_char != actual_char)
            current.append(min(insertion, deletion, substitution))
        previous = current

    return previous[-1] / max(len(expected), len(actual))
