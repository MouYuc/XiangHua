"""Tests for the media intake service.

The real ERes2NetV2 and Whisper models are deliberately not loaded here: the
tests target the decisions that are silent when wrong (safe session paths,
rights gating, VAD boundaries, speaker clustering, slice bounds, export
manifest). The model-backed smoke test lives in ``scripts/smoke_media_ingest.py``.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import soundfile as sf

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.infra.gpu import GPU_ARBITER
from backend.services import media_analysis, media_diarization, media_ingest, media_sessions

RATE = 16000


def tone(seconds: float, frequency: float, amplitude: float = 0.5, rate: int = RATE) -> np.ndarray:
    t = np.arange(int(seconds * rate), dtype=np.float32) / rate
    return (amplitude * np.sin(2 * np.pi * frequency * t)).astype(np.float32)


def silence(seconds: float, rate: int = RATE) -> np.ndarray:
    return np.zeros(int(seconds * rate), dtype=np.float32)


def two_speaker_audio(rate: int = RATE) -> np.ndarray:
    return np.concatenate(
        [
            silence(0.8, rate),
            tone(1.6, 180.0, rate=rate),
            silence(0.6, rate),
            tone(1.6, 320.0, rate=rate),
            silence(0.8, rate),
        ]
    )


class FakeEmbedder:
    """Two deterministic coordinates, one per frequency band."""

    def embed_audio(self, audio: np.ndarray, rate: int) -> np.ndarray:
        spectrum = np.abs(np.fft.rfft(audio * np.hanning(audio.size)))
        frequencies = np.fft.rfftfreq(audio.size, d=1.0 / rate)
        peak = float(frequencies[int(np.argmax(spectrum))]) if spectrum.size else 0.0
        return np.array([1.0, 0.0]) if peak < 250.0 else np.array([0.0, 1.0])


def wav_bytes(audio: np.ndarray, rate: int = RATE) -> io.BytesIO:
    buffer = io.BytesIO()
    sf.write(buffer, audio, rate, format="WAV", subtype="PCM_16")
    buffer.seek(0)
    return buffer


class SessionStorageTests(unittest.TestCase):
    def test_create_and_list_session(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = media_ingest.MediaIngestService(raw)
            session = service.create_session("第 3 集", rights_declared=True, rights_note="自有素材")
            self.assertTrue(session["session_id"].startswith("media_"))
            self.assertTrue((Path(raw) / "custom_data" / "media" / session["session_id"] / "session.json").is_file())
            rows = service.list_sessions()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["session_id"], session["session_id"])

    def test_rights_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = media_ingest.MediaIngestService(raw)
            with self.assertRaises(media_ingest.MediaIngestError):
                service.create_session("no rights", rights_declared=False)

    def test_session_id_cannot_escape_media_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = media_ingest.MediaIngestService(raw)
            with self.assertRaises(media_ingest.MediaIngestError):
                service.session_dir("../escape")

    def test_save_retries_while_a_reader_holds_the_file(self) -> None:
        """A UI poll reading session.json must not fail the next progress write.

        Windows refuses ``os.replace`` while another handle has the target open
        (WinError 5), and the analysis thread writes progress while the browser
        polls. The save must wait the reader out instead of failing the analysis.
        """
        with tempfile.TemporaryDirectory() as raw:
            service = media_ingest.MediaIngestService(raw)
            session = service.create_session("reader race", rights_declared=True)
            path = service.session_dir(session["session_id"]) / "session.json"
            session["name"] = "renamed while reading"
            errors: list[BaseException] = []

            def writer() -> None:
                try:
                    service.save(session)
                except BaseException as exc:
                    errors.append(exc)

            handle = path.open("r", encoding="utf-8")
            thread = threading.Thread(target=writer)
            thread.start()
            time.sleep(0.05)
            handle.close()
            thread.join(10)
            self.assertEqual(errors, [])
            self.assertEqual(service.load(session["session_id"])["name"], "renamed while reading")


class UploadTests(unittest.TestCase):
    def test_upload_extracts_both_analysis_rates(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = media_ingest.MediaIngestService(raw)
            session = service.create_session("clip", rights_declared=True)
            ingest = service.ingest_file(session["session_id"], "clip.wav", wav_bytes(two_speaker_audio()))
            source = ingest["source"]
            self.assertEqual(source["filename"], "clip.wav")
            self.assertAlmostEqual(source["duration_seconds"], 5.4, places=1)
            analysis = Path(raw) / "custom_data" / "media" / session["session_id"] / source["analysis_audio"]
            export = Path(raw) / "custom_data" / "media" / session["session_id"] / source["export_audio"]
            self.assertTrue(analysis.is_file())
            self.assertTrue(export.is_file())
            self.assertEqual(sf.info(str(analysis)).samplerate, media_ingest.ANALYSIS_SAMPLE_RATE)
            self.assertEqual(sf.info(str(export)).samplerate, media_ingest.EXPORT_SAMPLE_RATE)

    def test_a_session_accepts_only_one_media_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = media_ingest.MediaIngestService(raw)
            session = service.create_session("clip", rights_declared=True)
            service.ingest_file(session["session_id"], "clip.wav", wav_bytes(two_speaker_audio()))
            with self.assertRaises(media_ingest.MediaIngestError):
                service.ingest_file(session["session_id"], "second.wav", wav_bytes(two_speaker_audio()))

    def test_rejected_upload_removes_the_new_empty_session(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = media_ingest.MediaIngestService(raw)
            session = service.create_session("bad upload", rights_declared=True)
            session_dir = Path(raw) / "custom_data" / "media" / session["session_id"]
            self.assertTrue(session_dir.is_dir())
            with self.assertRaises(media_ingest.MediaIngestError):
                service.ingest_file(session["session_id"], "script.txt", io.BytesIO(b"not media"))
            self.assertFalse(session_dir.exists())
            self.assertEqual(service.list_sessions(), [])

    def test_failed_second_upload_never_deletes_a_session_with_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = media_ingest.MediaIngestService(raw)
            session = service.create_session("clip", rights_declared=True)
            service.ingest_file(session["session_id"], "clip.wav", wav_bytes(two_speaker_audio()))
            session_dir = Path(raw) / "custom_data" / "media" / session["session_id"]
            with self.assertRaises(media_ingest.MediaIngestError):
                service.ingest_file(session["session_id"], "second.txt", io.BytesIO(b"not media"))
            self.assertTrue(session_dir.is_dir())
            self.assertIsNotNone(service.load(session["session_id"])["source"])
            self.assertEqual(len(service.list_sessions()), 1)

    def test_io_failure_also_removes_the_new_empty_session(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = media_ingest.MediaIngestService(raw)
            session = service.create_session("disk error", rights_declared=True)
            session_dir = Path(raw) / "custom_data" / "media" / session["session_id"]
            # probe_media is looked up in media_sessions, where the upload
            # preparation that calls it now lives (media_ingest re-exports it).
            with patch.object(media_sessions, "probe_media", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    service.ingest_file(session["session_id"], "clip.wav", wav_bytes(two_speaker_audio()))
            self.assertFalse(session_dir.exists())
            self.assertEqual(service.list_sessions(), [])


class DiarizationTests(unittest.TestCase):
    def test_vad_finds_two_tones(self) -> None:
        regions = media_diarization.detect_speech_regions(two_speaker_audio(), RATE)
        self.assertEqual(len(regions), 2)
        self.assertLess(regions[0].start, 1.0)
        self.assertGreater(regions[0].end, 2.0)
        self.assertGreater(regions[1].start, 2.5)

    def test_two_speakers_are_clustered(self) -> None:
        result = media_diarization.diarize(
            two_speaker_audio(),
            RATE,
            FakeEmbedder().embed_audio,
            speaker_count=2,
        )
        self.assertEqual(result.speaker_count, 2)
        self.assertGreaterEqual(len(result.turns), 2)
        self.assertEqual({turn.speaker_index for turn in result.turns}, {0, 1})

    def test_long_turn_is_split_into_bounded_chunks(self) -> None:
        audio = tone(24.0, 220.0)
        turn = media_diarization.Turn(start=0.0, end=24.0, speaker_index=0)
        chunks = media_diarization.split_turn(audio, RATE, turn)
        self.assertGreaterEqual(len(chunks), 3)
        for start, end in chunks:
            self.assertGreaterEqual(end - start, media_diarization.MIN_SEGMENT_SECONDS - 0.01)
            self.assertLessEqual(end - start, media_diarization.MAX_SEGMENT_SECONDS + 0.01)
        self.assertAlmostEqual(chunks[-1][1], 24.0, places=2)


class AnalysisExclusivityTests(unittest.TestCase):
    """The HTTP layer builds a service per request; the lock must be process-wide."""

    def _session(self, service: media_ingest.MediaIngestService) -> str:
        return service.create_session("mutex", rights_declared=True)["session_id"]

    def test_running_analysis_rejects_second_call_and_cleans_registry(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = media_ingest.MediaIngestService(raw)
            session_id = self._session(service)
            entered = threading.Event()
            release = threading.Event()
            calls: list[str] = []

            def fake_worker(self, session_id, speaker_count, transcribe, model):
                calls.append(session_id)
                entered.set()
                release.wait(10.0)

            try:
                with patch.object(media_ingest.MediaIngestService, "_analysis_worker", fake_worker):
                    service.start_analysis(session_id)
                    self.assertTrue(entered.wait(5.0), "analysis worker did not start")
                    # Simulate the original race: the persisted job marker is not
                    # yet visible to the other request. Only the process-wide
                    # registry can reject it now.
                    stale = service.load(session_id)
                    stale["job"]["status"] = "idle"
                    service.save(stale)
                    with self.assertRaises(media_ingest.MediaIngestError):
                        service.start_analysis(session_id)
                    with self.assertRaises(media_ingest.MediaIngestError):
                        media_ingest.MediaIngestService(raw).start_analysis(session_id)
                    self.assertEqual(calls, [session_id])
            finally:
                release.set()

            deadline = time.monotonic() + 5.0
            while session_id in media_ingest._ACTIVE_ANALYSES and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertNotIn(session_id, media_ingest._ACTIVE_ANALYSES)

    def test_concurrent_callers_have_exactly_one_winner(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            session_id = self._session(media_ingest.MediaIngestService(raw))
            release = threading.Event()
            entered = threading.Event()
            barrier = threading.Barrier(2)
            outcomes: list[str] = []
            calls: list[str] = []

            def fake_worker(self, session_id, speaker_count, transcribe, model):
                calls.append(session_id)
                entered.set()
                release.wait(10.0)

            def attempt() -> None:
                service = media_ingest.MediaIngestService(raw)
                barrier.wait(5.0)
                try:
                    service.start_analysis(session_id)
                    outcomes.append("started")
                except media_ingest.MediaIngestError:
                    outcomes.append("rejected")

            try:
                with patch.object(media_ingest.MediaIngestService, "_analysis_worker", fake_worker):
                    threads = [threading.Thread(target=attempt) for _ in range(2)]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(5.0)
                    self.assertTrue(entered.wait(5.0), "analysis worker did not start")
                    self.assertEqual(sorted(outcomes), ["rejected", "started"])
                    self.assertEqual(calls, [session_id])
            finally:
                release.set()

            deadline = time.monotonic() + 5.0
            while session_id in media_ingest._ACTIVE_ANALYSES and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertNotIn(session_id, media_ingest._ACTIVE_ANALYSES)


class TranscriptNormalizationTests(unittest.TestCase):
    def test_traditional_whisper_text_is_written_as_simplified(self) -> None:
        class FakeWhisper:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict]] = []

            def transcribe(self, path: str, **kwargs):
                self.calls.append((path, kwargs))
                return [SimpleNamespace(text="這是"), SimpleNamespace(text="繁體字")], SimpleNamespace(language="zh")

        with tempfile.TemporaryDirectory() as raw:
            service = media_ingest.MediaIngestService(raw)
            session = service.create_session("asr", rights_declared=True, language="zh")
            segments = [{"segment_id": "seg_0001", "path": "segments/seg_0001.wav", "text": ""}]
            whisper = FakeWhisper()
            # _load_whisper is looked up in media_analysis, where the transcription
            # pipeline that calls it now lives (media_ingest re-exports it).
            with patch.object(media_analysis, "_load_whisper", return_value=whisper):
                service._transcribe(session["session_id"], segments, model="tiny", language="zh")
            self.assertEqual(segments[0]["text"], "这是繁体字")
            self.assertNotIn("text_error", segments[0])
            self.assertEqual(whisper.calls[0][1]["language"], "zh")

    def test_cantonese_keeps_traditional_characters(self) -> None:
        """OpenCC t2s would turn 係 into 系, which is not the Cantonese word."""

        class FakeWhisper:
            def transcribe(self, path: str, **kwargs):
                return [SimpleNamespace(text="係唔係")], SimpleNamespace(language="zh")

        with tempfile.TemporaryDirectory() as raw:
            service = media_ingest.MediaIngestService(raw)
            session = service.create_session("asr", rights_declared=True, language="yue")
            segments = [{"segment_id": "seg_0001", "path": "segments/seg_0001.wav", "text": ""}]
            with patch.object(media_analysis, "_load_whisper", return_value=FakeWhisper()):
                service._transcribe(session["session_id"], segments, model="tiny", language="yue")
            self.assertEqual(segments[0]["text"], "係唔係")


class EndToEndTests(unittest.TestCase):
    def _prepared_session(self, service: media_ingest.MediaIngestService) -> dict:
        session = service.create_session("two speakers", rights_declared=True)
        service.ingest_file(session["session_id"], "clip.wav", wav_bytes(two_speaker_audio()))
        return service.analyze(session["session_id"], speaker_count=2, transcribe=False)

    def test_analysis_produces_qc_segments(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = media_ingest.MediaIngestService(raw, embedder=FakeEmbedder())
            session = self._prepared_session(service)
            self.assertEqual(len(session["speakers"]), 2)
            self.assertGreater(len(session["segments"]), 0)
            for speaker in session["speakers"]:
                self.assertEqual(
                    speaker["segment_count"],
                    sum(1 for segment in session["segments"] if segment["speaker_id"] == speaker["speaker_id"]),
                )
            for segment in session["segments"]:
                self.assertIn("qc", segment)
                self.assertIn(segment["qc"]["status"], {"ok", "warn", "fail"})
                self.assertGreater(segment["duration_seconds"], 0)
                self.assertTrue((Path(raw) / "custom_data" / "media" / session["session_id"] / segment["path"]).is_file())

    def test_export_writes_list_manifest_and_checksums(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = media_ingest.MediaIngestService(raw, embedder=FakeEmbedder())
            session = self._prepared_session(service)
            target = session["speakers"][0]["speaker_id"]
            for segment in session["segments"]:
                if segment["speaker_id"] == target:
                    segment["text"] = "测试文本"
            service.save(session)
            dataset = service.export_dataset(
                session["session_id"], speaker_id=target, include_failed=True, require_text=True
            )
            base = Path(raw) / "custom_data" / "media" / session["session_id"]
            self.assertTrue((base / dataset["list"]).is_file())
            self.assertTrue((base / dataset["manifest"]).is_file())
            self.assertTrue((base / dataset["checksums"]).is_file())
            manifest = __import__("json").loads((base / dataset["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["speaker_id"], target)
            self.assertEqual(manifest["stats"]["segment_count"], len(manifest["segments"]))
            self.assertTrue(manifest["train_ready"])
            self.assertEqual(len((base / dataset["list"]).read_text(encoding="utf-8").strip().splitlines()), len(manifest["segments"]))


    def test_activate_copies_slices_and_backs_up_previous_list(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = media_ingest.MediaIngestService(raw, embedder=FakeEmbedder())
            session = self._prepared_session(service)
            target = session["speakers"][0]["speaker_id"]
            for segment in session["segments"]:
                if segment["speaker_id"] == target:
                    segment["text"] = "测试文本"
            service.save(session)
            asr = Path(raw) / "custom_data" / "asr"
            asr.mkdir(parents=True, exist_ok=True)
            previous = asr / "sliced_corrected.list"
            previous.write_text("old|sliced|ZH|旧数据\n", encoding="utf-8")
            result = service.activate_dataset(
                session["session_id"], speaker_id=target, include_failed=True, require_text=True
            )
            self.assertGreater(result["copied"], 0)
            self.assertTrue(Path(result["list"]).is_file())
            self.assertTrue(Path(result["backup"]).is_file())
            self.assertTrue(result["text_ready"])
            self.assertTrue(Path(result["whisper_list"]).is_file())
            lines = Path(result["list"]).read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), result["copied"])
            for line in lines:
                self.assertIn("|sliced|", line)
                self.assertTrue(line.split("|")[3].strip())
                self.assertTrue(Path(line.split("|")[0]).is_file())

    def test_export_requires_text_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = media_ingest.MediaIngestService(raw, embedder=FakeEmbedder())
            session = self._prepared_session(service)
            with self.assertRaises(media_ingest.MediaIngestError):
                service.export_dataset(session["session_id"], speaker_id=session["speakers"][0]["speaker_id"])

    def test_select_speaker_rejects_unknown_id(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = media_ingest.MediaIngestService(raw, embedder=FakeEmbedder())
            session = self._prepared_session(service)
            with self.assertRaises(media_ingest.MediaIngestError):
                service.select_speaker(session["session_id"], "spk_99")


class RunVersioningTests(unittest.TestCase):
    """Analysis runs are append-only, and a dataset can be replayed by id."""

    def setUp(self) -> None:
        GPU_ARBITER.reset_for_tests()

    def tearDown(self) -> None:
        GPU_ARBITER.reset_for_tests()

    def _service(self, raw: str) -> media_ingest.MediaIngestService:
        return media_ingest.MediaIngestService(raw, embedder=FakeEmbedder())

    def _prepared(self, service: media_ingest.MediaIngestService) -> dict:
        session = service.create_session("版本化", rights_declared=True)
        service.ingest_file(session["session_id"], "clip.wav", wav_bytes(two_speaker_audio()))
        return service.analyze(session["session_id"], speaker_count=2, transcribe=False)

    @staticmethod
    def _sha_map(directory: Path) -> dict[str, str]:
        return {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.glob("seg_*.wav"))
        }

    def _write_legacy_session(self, raw: str) -> tuple[Path, dict]:
        """A session.json written before analysis versioning existed."""
        session_id = "media_legacy_manual"
        base = Path(raw) / "custom_data" / "media" / session_id
        segments_dir = base / "segments"
        segments_dir.mkdir(parents=True)
        segments = []
        for index in range(1, 3):
            path = segments_dir / f"seg_{index:04d}.wav"
            sf.write(
                str(path),
                tone(0.8, 200.0 + 40 * index, rate=media_ingest.EXPORT_SAMPLE_RATE),
                media_ingest.EXPORT_SAMPLE_RATE,
                subtype="PCM_16",
            )
            segments.append(
                {
                    "segment_id": f"seg_{index:04d}",
                    "speaker_id": "spk_01",
                    "start": round((index - 1) * 1.2, 3),
                    "end": round((index - 1) * 1.2 + 0.8, 3),
                    "duration_seconds": 0.8,
                    "path": f"segments/seg_{index:04d}.wav",
                    "audio_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "text": f"旧文本{index}",
                    "qc": {"status": "ok", "blocking": [], "warnings": [], "metrics": {}},
                }
            )
        analysis_dir = base / "analysis"
        analysis_dir.mkdir(parents=True)
        sf.write(str(analysis_dir / "source_16k.wav"), two_speaker_audio(), RATE, subtype="PCM_16")
        sf.write(
            str(analysis_dir / "source_32k.wav"),
            two_speaker_audio(media_ingest.EXPORT_SAMPLE_RATE),
            media_ingest.EXPORT_SAMPLE_RATE,
            subtype="PCM_16",
        )
        payload = {
            "schema_version": 1,
            "session_id": session_id,
            "name": "旧会话",
            "language": "zh",
            "created_at": "2025-01-01T00:00:00+08:00",
            "updated_at": "2025-01-02T00:00:00+08:00",
            "rights": {"declared": True, "note": "自有素材", "declared_at": "2025-01-01T00:00:00+08:00"},
            "source": {
                "filename": "old.wav",
                "path": "source/old.wav",
                "analysis_audio": "analysis/source_16k.wav",
                "export_audio": "analysis/source_32k.wav",
            },
            "job": {"status": "done", "stage": "完成", "progress": 1.0, "message": "", "error": ""},
            "speakers": [{"speaker_id": "spk_01", "segment_count": 2}],
            "segments": segments,
            "selected_speaker": "spk_01",
            "dataset": None,
        }
        (base / "session.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return base, payload

    def test_rerunning_analysis_appends_a_run_and_keeps_every_slice(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = self._service(raw)
            first = self._prepared(service)
            session_id = first["session_id"]
            first_run = first["active_run"]
            self.assertEqual(len(first["runs"]), 1)
            self.assertEqual(first["runs"][0]["run_id"], first_run)
            self.assertEqual(first["runs"][0]["path"], f"segments/{first_run}")
            self.assertEqual(first["runs"][0]["segment_count"], len(first["segments"]))
            self.assertTrue(
                all(segment["path"].startswith(f"segments/{first_run}/") for segment in first["segments"])
            )
            first_dir = service.session_dir(session_id) / "segments" / first_run
            snapshot = json.loads((first_dir / "segments.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [segment["segment_id"] for segment in snapshot],
                [segment["segment_id"] for segment in first["segments"]],
            )
            before = self._sha_map(first_dir)
            self.assertGreater(len(before), 0)

            second = service.analyze(session_id, speaker_count=2, transcribe=False)
            second_run = second["active_run"]
            self.assertNotEqual(first_run, second_run)
            self.assertEqual([run["run_id"] for run in second["runs"]], [first_run, second_run])
            self.assertTrue(
                all(segment["path"].startswith(f"segments/{second_run}/") for segment in second["segments"])
            )
            self.assertTrue((service.session_dir(session_id) / "segments" / second_run / "segments.json").is_file())
            # The first run's slices are byte-identical and still on disk.
            self.assertEqual(self._sha_map(first_dir), before)
            # Nothing was written to the pre-versioning flat layout.
            self.assertEqual(list((service.session_dir(session_id) / "segments").glob("seg_*.wav")), [])
            reloaded = service.load(session_id)
            self.assertEqual(reloaded["active_run"], second_run)
            self.assertEqual(len(reloaded["runs"]), 2)

    def test_export_reads_the_frozen_segments_json_of_an_old_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = self._service(raw)
            first = self._prepared(service)
            session_id = first["session_id"]
            first_run = first["active_run"]
            first_segments = [dict(segment) for segment in first["segments"]]
            second = service.analyze(session_id, speaker_count=2, transcribe=False)
            target = first["speakers"][0]["speaker_id"]

            dataset = service.export_dataset(
                session_id,
                speaker_id=target,
                include_failed=True,
                require_text=False,
                run_id=first_run,
            )
            self.assertEqual(dataset["run_id"], first_run)
            base = service.session_dir(session_id)
            manifest = json.loads((base / dataset["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["run_id"], first_run)
            expected = [segment for segment in first_segments if segment["speaker_id"] == target]
            self.assertEqual([row["audio"] for row in manifest["segments"]], [segment["path"] for segment in expected])
            self.assertTrue(all(first_run in row["audio"] for row in manifest["segments"]))
            list_text = (base / dataset["list"]).read_text(encoding="utf-8")
            self.assertNotIn(second["active_run"], list_text)

            # No run_id means the active run (the second analysis).
            active_dataset = service.export_dataset(
                session_id, speaker_id=target, include_failed=True, require_text=False
            )
            self.assertEqual(active_dataset["run_id"], second["active_run"])

            with self.assertRaises(media_ingest.MediaIngestError):
                service.export_dataset(
                    session_id,
                    speaker_id=target,
                    include_failed=True,
                    require_text=False,
                    run_id="run_missing",
                )

    def test_activate_dataset_by_id_after_reanalysis_replays_the_old_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = self._service(raw)
            session = self._prepared(service)
            session_id = session["session_id"]
            target = session["speakers"][0]["speaker_id"]
            for segment in session["segments"]:
                if segment["speaker_id"] == target:
                    segment["text"] = "旧训练集"
            service.save(session)
            dataset = service.export_dataset(
                session_id, speaker_id=target, include_failed=True, require_text=True
            )
            old_run = dataset["run_id"]
            base = service.session_dir(session_id)
            manifest = json.loads((base / dataset["manifest"]).read_text(encoding="utf-8"))
            expected = [(row["segment_id"], row["sha256"]) for row in manifest["segments"]]
            self.assertGreater(len(expected), 0)

            reanalyzed = service.analyze(session_id, speaker_count=2, transcribe=False)
            self.assertNotEqual(reanalyzed["active_run"], old_run)
            self.assertIsNone(reanalyzed["dataset"])

            asr = Path(raw) / "custom_data" / "asr"
            asr.mkdir(parents=True, exist_ok=True)
            (asr / "sliced_corrected.list").write_text("old|sliced|ZH|旧数据\n", encoding="utf-8")

            result = service.activate_dataset(session_id, dataset_id=dataset["dataset_id"])
            self.assertEqual(result["dataset_id"], dataset["dataset_id"])
            self.assertEqual(result["run_id"], old_run)
            self.assertEqual(result["copied"], len(expected))
            self.assertTrue(Path(result["backup"]).is_file())
            self.assertEqual(Path(result["backup"]).read_text(encoding="utf-8").strip(), "old|sliced|ZH|旧数据")

            lines = Path(result["list"]).read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), len(expected))
            for segment_id, sha in expected:
                destination = Path(raw) / "custom_data" / "sliced" / f"media_{session_id}_{segment_id}.wav"
                self.assertTrue(destination.is_file())
                self.assertEqual(hashlib.sha256(destination.read_bytes()).hexdigest(), sha)
                self.assertIn(str(destination), [line.split("|")[0] for line in lines])
            reloaded = service.load(session_id)
            self.assertEqual(reloaded["dataset"]["dataset_id"], dataset["dataset_id"])
            self.assertEqual(reloaded["dataset"]["run_id"], old_run)

            with self.assertRaises(media_ingest.MediaIngestError):
                service.activate_dataset(session_id, dataset_id="ds_does_not_exist")

    def test_legacy_session_exports_and_activates_without_rewriting_disk(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base, payload = self._write_legacy_session(raw)
            session_path = base / "session.json"
            before = session_path.read_bytes()
            service = self._service(raw)
            self.assertEqual(len(service.list_sessions()), 1)

            loaded = service.load(payload["session_id"])
            self.assertEqual(loaded["active_run"], media_ingest.LEGACY_RUN_ID)
            self.assertEqual([run["run_id"] for run in loaded["runs"]], [media_ingest.LEGACY_RUN_ID])
            self.assertEqual(loaded["runs"][0]["path"], "segments")
            self.assertEqual(loaded["runs"][0]["segment_count"], 2)
            self.assertEqual(session_path.read_bytes(), before, "load() must not rewrite the legacy session")
            self.assertTrue((base / "segments" / "seg_0001.wav").is_file())
            self.assertFalse((base / "segments" / media_ingest.LEGACY_RUN_ID).exists())

            dataset = service.export_dataset(payload["session_id"])
            self.assertEqual(dataset["run_id"], media_ingest.LEGACY_RUN_ID)
            manifest = json.loads((base / dataset["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["run_id"], media_ingest.LEGACY_RUN_ID)
            self.assertEqual(
                [row["audio"] for row in manifest["segments"]],
                ["segments/seg_0001.wav", "segments/seg_0002.wav"],
            )

            activated = service.activate_dataset(payload["session_id"], run_id=media_ingest.LEGACY_RUN_ID)
            self.assertEqual(activated["run_id"], media_ingest.LEGACY_RUN_ID)
            self.assertEqual(activated["dataset_id"], "")
            self.assertEqual(activated["copied"], 2)
            self.assertTrue(activated["text_ready"])

            replayed = service.activate_dataset(payload["session_id"], dataset_id=dataset["dataset_id"])
            self.assertEqual(replayed["dataset_id"], dataset["dataset_id"])
            self.assertEqual(replayed["run_id"], media_ingest.LEGACY_RUN_ID)
            self.assertEqual(replayed["copied"], 2)

    def test_legacy_run_stays_exportable_after_a_versioned_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base, payload = self._write_legacy_session(raw)
            service = self._service(raw)
            analyzed = service.analyze(payload["session_id"], speaker_count=2)
            self.assertEqual([run["run_id"] for run in analyzed["runs"]][0], media_ingest.LEGACY_RUN_ID)
            self.assertEqual(len(analyzed["runs"]), 2)
            snapshot = base / "segments" / "segments.json"
            self.assertTrue(snapshot.is_file())
            self.assertEqual(
                [row["segment_id"] for row in json.loads(snapshot.read_text(encoding="utf-8"))],
                ["seg_0001", "seg_0002"],
            )
            dataset = service.export_dataset(
                payload["session_id"],
                speaker_id="spk_01",
                run_id=media_ingest.LEGACY_RUN_ID,
            )
            self.assertEqual(dataset["run_id"], media_ingest.LEGACY_RUN_ID)
            manifest = json.loads((base / dataset["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(
                [row["audio"] for row in manifest["segments"]],
                ["segments/seg_0001.wav", "segments/seg_0002.wav"],
            )
            self.assertTrue((base / "segments" / "seg_0001.wav").is_file())


class GpuLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        GPU_ARBITER.reset_for_tests()

    def tearDown(self) -> None:
        GPU_ARBITER.reset_for_tests()

    def test_analyze_holds_the_media_analysis_lease(self) -> None:
        class LeaseProbeEmbedder:
            def __init__(self) -> None:
                self.snapshots: list[dict] = []
                self.delegate = FakeEmbedder()

            def embed_audio(self, audio: np.ndarray, rate: int) -> np.ndarray:
                self.snapshots.append(GPU_ARBITER.status())
                return self.delegate.embed_audio(audio, rate)

        with tempfile.TemporaryDirectory() as raw:
            probe = LeaseProbeEmbedder()
            service = media_ingest.MediaIngestService(raw, embedder=probe)
            session = service.create_session("租约探针", rights_declared=True)
            service.ingest_file(session["session_id"], "clip.wav", wav_bytes(two_speaker_audio()))
            service.analyze(session["session_id"], speaker_count=2)

            self.assertTrue(probe.snapshots)
            holders = [snapshot["holder"] for snapshot in probe.snapshots if snapshot.get("busy")]
            self.assertTrue(holders)
            for holder in holders:
                self.assertEqual(holder["kind"], "media_analysis")
                self.assertEqual(holder["job_id"], session["session_id"])
                self.assertEqual(holder["label"], f"媒体分析 {session['name']}")
            self.assertFalse(GPU_ARBITER.status()["busy"])

    def test_lease_is_released_when_analysis_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = media_ingest.MediaIngestService(raw, embedder=FakeEmbedder())
            session = service.create_session("失败分析", rights_declared=True)
            service.ingest_file(session["session_id"], "silence.wav", wav_bytes(silence(2.0)))
            with self.assertRaises(media_ingest.MediaIngestError):
                service.analyze(session["session_id"], speaker_count=2)
            self.assertFalse(GPU_ARBITER.status()["busy"])


class ApiTests(unittest.TestCase):
    def _client(self):
        from fastapi.testclient import TestClient

        from backend import app as app_module

        return TestClient(app_module.create_app())

    def test_upload_requires_rights(self) -> None:
        from unittest.mock import patch

        from backend.api import deps

        with tempfile.TemporaryDirectory() as raw:
            service = media_ingest.MediaIngestService(raw, embedder=FakeEmbedder())
            with patch.object(deps, "_media_service", return_value=service):
                client = self._client()
                response = client.post(
                    "/api/media/sessions",
                    files={"recording": ("clip.wav", wav_bytes(two_speaker_audio()), "audio/wav")},
                    data={"rights_declared": "false"},
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("有权", response.json()["detail"])

    def test_upload_analyze_select_and_export(self) -> None:
        import time
        from unittest.mock import patch

        from backend.api import deps

        with tempfile.TemporaryDirectory() as raw:
            service = media_ingest.MediaIngestService(raw, embedder=FakeEmbedder())
            with patch.object(deps, "_media_service", return_value=service):
                client = self._client()
                response = client.post(
                    "/api/media/sessions",
                    files={"recording": ("clip.wav", wav_bytes(two_speaker_audio()), "audio/wav")},
                    data={"rights_declared": "true", "rights_note": "自有素材", "name": "测试"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                session_id = response.json()["session"]["session_id"]
                # Upload transcode and analysis both run on workers now, so the
                # session walks uploading -> running -> done behind the response.
                job = {"status": "timeout"}
                for _ in range(200):
                    job = client.get(f"/api/media/sessions/{session_id}/job").json()["job"]
                    if job["status"] in {"done", "fail"}:
                        break
                    time.sleep(0.05)
                self.assertEqual(job["status"], "done", job)
                session = client.get(f"/api/media/sessions/{session_id}").json()["session"]
                self.assertEqual(len(session["speakers"]), 2)
                speaker_id = session["speakers"][0]["speaker_id"]
                selected = client.post(
                    f"/api/media/sessions/{session_id}/select-speaker",
                    json={"speaker_id": speaker_id},
                )
                self.assertEqual(selected.status_code, 200, selected.text)
                exported = client.post(
                    f"/api/media/sessions/{session_id}/export",
                    json={"speaker_id": speaker_id, "include_failed": True, "require_text": False},
                )
                self.assertEqual(exported.status_code, 200, exported.text)
                self.assertTrue(exported.json()["dataset"]["manifest_url"])
                activated = client.post(
                    f"/api/media/sessions/{session_id}/activate",
                    json={"speaker_id": speaker_id, "include_failed": True, "require_text": False},
                )
                self.assertEqual(activated.status_code, 200, activated.text)
                self.assertGreater(activated.json()["copied"], 0)



if __name__ == "__main__":
    unittest.main()
