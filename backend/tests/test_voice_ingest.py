"""Tests for the one-upload natural-speech onboarding path."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf
from fastapi.testclient import TestClient

from backend import app as app_module
from backend.services import core
from backend.services import voice_ingest as voice_ingest_module

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services import voice_ingest

RATE = 16000


def speech_like(seconds: float, *, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * RATE)) / RATE
    carrier = 0.2 * np.sin(2 * np.pi * 180 * t + 0.4 * np.sin(2 * np.pi * 3 * t))
    gate = (np.sin(2 * np.pi * 2.4 * t) > -0.25).astype(np.float64)
    return (carrier * gate + rng.normal(0, 1e-4, t.size)).astype(np.float32)


def write_source(path: Path, audio: np.ndarray) -> Path:
    sf.write(str(path), audio, RATE)
    return path


class TranscriptNormalizationTests(unittest.TestCase):
    def test_traditional_chinese_is_converted_to_simplified(self) -> None:
        self.assertEqual(
            voice_ingest.normalize_transcript("今天的天氣真不錯，我們出去走走吧", "zh"),
            "今天的天气真不错，我们出去走走吧",
        )

    def test_non_chinese_is_left_alone(self) -> None:
        self.assertEqual(voice_ingest.normalize_transcript("Hello, world.", "en"), "Hello, world.")

    def test_cantonese_keeps_traditional_characters(self) -> None:
        # OpenCC t2s rewrites 係 as 系, which is a different Cantonese word, so the
        # product decision is to leave yue transcripts in traditional characters.
        self.assertEqual(voice_ingest.normalize_transcript("係唔係", "yue"), "係唔係")


class VoiceIngestTests(unittest.TestCase):
    def test_candidate_selection_finds_speech_inside_silence(self) -> None:
        audio = np.concatenate([
            np.zeros(RATE, dtype=np.float32),
            speech_like(3.0),
            np.zeros(RATE, dtype=np.float32),
        ])
        windows = voice_ingest.candidate_windows(audio, RATE)
        self.assertTrue(windows)
        self.assertTrue(any(start >= 0.5 and end <= 4.5 for start, end in windows))

    def test_silence_has_no_candidates(self) -> None:
        self.assertEqual(voice_ingest.candidate_windows(np.zeros(RATE * 3, dtype=np.float32), RATE), [])

    def test_prepare_returns_selected_clip_and_auto_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = write_source(root / "natural.wav", speech_like(8.0))
            result = voice_ingest.prepare_reference_from_audio(
                source,
                root / "prepared",
                transcriber=lambda path: {"text": "今天天气很好。", "language": "zh"},
                judge=lambda path: {
                    "status": "ok",
                    "metrics": {"estimated_snr_db": 30.0, "silence_ratio": 0.1},
                    "warnings": [],
                },
            )
            selected = Path(result["ref_audio"])
            self.assertTrue(selected.is_file())
            self.assertEqual(result["ref_text"], "今天天气很好。")
            self.assertLessEqual(result["duration_seconds"], voice_ingest.MAX_SELECTED_SECONDS)
            self.assertGreaterEqual(result["duration_seconds"], voice_ingest.MIN_SELECTED_SECONDS)
            self.assertGreater(result["selected"]["candidate_count"], 0)

    def test_empty_transcript_is_a_user_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = write_source(root / "natural.wav", speech_like(5.0))
            with self.assertRaises(voice_ingest.VoiceIngestError):
                voice_ingest.prepare_reference_from_audio(
                    source,
                    root / "prepared",
                    transcriber=lambda path: {"text": "   "},
                    judge=lambda path: {"status": "ok", "metrics": {}, "warnings": []},
                )

    def test_missing_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(voice_ingest.VoiceIngestError):
                voice_ingest.prepare_reference_from_audio(Path(raw) / "missing.wav", Path(raw) / "out")


class VoiceIngestRouteTests(unittest.TestCase):
    def test_upload_auto_prepares_path_and_transcript(self) -> None:
        prepared = {
            "ref_audio": "G:/prepared/voice_ref.wav",
            "ref_text": "今天天气很好。",
            "selected": {"start": 1.0, "end": 6.0, "duration": 5.0, "candidate_count": 2, "score": 99.0},
            "transcription": {"text": "今天天气很好。", "language": "zh"},
            "warnings": [],
            "quality": {"status": "ok", "metrics": {}},
        }
        with tempfile.TemporaryDirectory() as raw:
            with patch.object(core, "REFERENCE_AUDIO_DIR", Path(raw)), patch.object(
                voice_ingest_module, "prepare_reference_from_audio", return_value=prepared
            ):
                client = TestClient(app_module.create_app())
                response = client.post(
                    "/api/reference-audio",
                    data={"purpose": "synthesis", "auto_prepare": "true", "language": "zh"},
                    files={"audio": ("natural.wav", b"audio", "audio/wav")},
                )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["auto_prepared"])
        self.assertEqual(data["path"], prepared["ref_audio"])
        self.assertEqual(data["ref_text"], "今天天气很好。")

    def test_upload_can_still_use_the_legacy_inspect_only_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with patch.object(core, "REFERENCE_AUDIO_DIR", Path(raw)):
                client = TestClient(app_module.create_app())
                response = client.post(
                    "/api/reference-audio",
                    data={"purpose": "synthesis", "auto_prepare": "false"},
                    files={"audio": ("natural.wav", b"not audio", "audio/wav")},
                )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["auto_prepared"])
        self.assertEqual(data["ref_text"], "")


if __name__ == "__main__":
    unittest.main()
