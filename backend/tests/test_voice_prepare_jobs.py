"""Tests for background voice preparation jobs and their progress reporting.

These tests use injected fake preparation functions, so no Whisper model, speaker
model or DSP gate is loaded. The route tests only verify the HTTP contract around
the manager.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services import voice_ingest  # noqa: E402
from backend.services.voice_prepare_jobs import (  # noqa: E402
    MAX_RETAINED_JOBS,
    VoicePrepareJobManager,
)

SNAPSHOT_FIELDS = {
    "job_id",
    "status",
    "phase",
    "progress",
    "stage",
    "result",
    "error",
    "message",
    "started_at",
    "updated_at",
}
TERMINAL_STATUSES = {"done", "failed"}


def wait_for_terminal(manager: VoicePrepareJobManager, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = manager.snapshot(job_id)
        if snapshot is not None and snapshot["status"] in TERMINAL_STATUSES:
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"任务 {job_id} 没有在 {timeout} 秒内结束：{manager.snapshot(job_id)}")


def wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def instant_prepare(text: str = "今天天气很好。", duration: float = 5.2):
    def fake(source, output_dir, *, language="zh", progress=None):
        if progress is not None:
            progress(0.5, "逐个候选质检")
        return {
            "ref_audio": str(Path(output_dir) / "voice_ref_test.wav"),
            "ref_text": text,
            "duration_seconds": duration,
            "warnings": [],
            "quality": {"status": "ok", "purpose": "synthesis"},
        }

    return fake


def speech_like(seconds: float, *, rate: int = 16000, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * rate)) / rate
    carrier = 0.2 * np.sin(2 * np.pi * 180 * t + 0.4 * np.sin(2 * np.pi * 3 * t))
    gate = (np.sin(2 * np.pi * 2.4 * t) > -0.25).astype(np.float64)
    return (carrier * gate + rng.normal(0, 1e-4, t.size)).astype(np.float32)


class VoicePrepareJobSuccessTests(unittest.TestCase):
    def test_success_snapshot_has_required_fields_and_result(self) -> None:
        manager = VoicePrepareJobManager()
        entered = threading.Event()
        release = threading.Event()

        def fake_prepare(source, output_dir, *, language="zh", progress=None):
            if progress is not None:
                progress(0.35, "逐个候选质检")
            entered.set()
            release.wait(5)
            return {
                "ref_audio": str(Path(output_dir) / "voice_ref_abc.wav"),
                "ref_text": "今天天气很好。",
                "duration_seconds": 5.2,
                "selected": {"start": 0.0, "end": 5.2, "duration": 5.2},
                "warnings": [],
                "quality": {"status": "ok", "purpose": "synthesis"},
            }

        snapshot = manager.start(Path("natural.wav"), Path("prepared"), prepare=fake_prepare)
        self.assertIn(snapshot["status"], {"queued", "running"})
        self.assertTrue(entered.wait(5))
        running = manager.snapshot(snapshot["job_id"])
        self.assertIsNotNone(running)
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["phase"], "prepare")
        self.assertAlmostEqual(running["progress"], 0.35)
        self.assertEqual(running["stage"], "逐个候选质检")
        self.assertEqual(running["error"], "")

        release.set()
        done = wait_for_terminal(manager, snapshot["job_id"])
        self.assertTrue(SNAPSHOT_FIELDS.issubset(done.keys()))
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["phase"], "done")
        self.assertEqual(done["progress"], 1.0)
        self.assertEqual(done["stage"], "完成")
        self.assertEqual(done["error"], "")
        self.assertIn("今天天气很好。", done["result"]["ref_text"])
        self.assertIn("已选取 5.2 秒语音", done["message"])
        for field in ("started_at", "updated_at"):
            self.assertIsInstance(done[field], str)
            self.assertTrue(done[field])

    def test_snapshots_are_copies_of_the_job_state(self) -> None:
        manager = VoicePrepareJobManager()
        snapshot = manager.start(Path("natural.wav"), Path("prepared"), prepare=instant_prepare())
        done = wait_for_terminal(manager, snapshot["job_id"])
        done["result"]["ref_text"] = "被外部篡改"
        done["status"] = "failed"
        fresh = manager.snapshot(snapshot["job_id"])
        self.assertEqual(fresh["status"], "done")
        self.assertEqual(fresh["result"]["ref_text"], "今天天气很好。")

    def test_progress_values_are_clamped(self) -> None:
        manager = VoicePrepareJobManager()

        def fake_prepare(source, output_dir, *, language="zh", progress=None):
            assert progress is not None
            progress(3.5, "越界进度")
            return {"ref_audio": "ref.wav", "ref_text": "文本", "warnings": []}

        snapshot = manager.start(Path("natural.wav"), Path("prepared"), prepare=fake_prepare)
        done = wait_for_terminal(manager, snapshot["job_id"])
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["progress"], 1.0)


class VoicePrepareJobFailureTests(unittest.TestCase):
    def test_voice_ingest_error_message_is_passed_through_unchanged(self) -> None:
        manager = VoicePrepareJobManager()
        chinese = "没有检测到可用语音；请确认录音里有正常说话声。"

        def fake_prepare(source, output_dir, *, language="zh", progress=None):
            raise voice_ingest.VoiceIngestError(chinese)

        snapshot = manager.start(Path("natural.wav"), Path("prepared"), prepare=fake_prepare)
        failed = wait_for_terminal(manager, snapshot["job_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["phase"], "failed")
        self.assertEqual(failed["stage"], "失败")
        self.assertEqual(failed["error"], chinese)
        self.assertEqual(failed["message"], chinese)
        self.assertIsNone(failed["result"])

    def test_unexpected_error_keeps_the_exception_class_and_message(self) -> None:
        manager = VoicePrepareJobManager()

        def fake_prepare(source, output_dir, *, language="zh", progress=None):
            raise RuntimeError("磁盘写满")

        snapshot = manager.start(Path("natural.wav"), Path("prepared"), prepare=fake_prepare)
        failed = wait_for_terminal(manager, snapshot["job_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "RuntimeError: 磁盘写满")
        self.assertEqual(failed["message"], "磁盘写满")

    def test_non_dict_result_is_treated_as_a_failure(self) -> None:
        manager = VoicePrepareJobManager()
        snapshot = manager.start(Path("natural.wav"), Path("prepared"), prepare=lambda *a, **k: "not-a-dict")
        failed = wait_for_terminal(manager, snapshot["job_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("字典", failed["error"])

    def test_unknown_job_has_no_snapshot(self) -> None:
        manager = VoicePrepareJobManager()
        self.assertIsNone(manager.snapshot("voice_prep_missing"))
        self.assertIsNone(manager.wait("voice_prep_missing", 0.05))
        self.assertIsNone(manager.update("voice_prep_missing", {"status": "done"}))


class VoicePrepareJobConcurrencyTests(unittest.TestCase):
    def test_concurrent_duplicate_submissions_do_not_overwrite_each_other(self) -> None:
        manager = VoicePrepareJobManager()
        barrier = threading.Barrier(2)
        entered = [threading.Event(), threading.Event()]
        release = threading.Event()

        def make_prepare(index: int):
            def fake_prepare(source, output_dir, *, language="zh", progress=None):
                if progress is not None:
                    progress(0.4 + 0.1 * index, f"候选 {index}")
                entered[index].set()
                release.wait(5)
                return {
                    "ref_audio": f"voice_ref_{index}.wav",
                    "ref_text": f"文本 {index}",
                    "duration_seconds": 4.0,
                    "warnings": [],
                }

            return fake_prepare

        started: dict[int, dict] = {}

        def submit(index: int) -> None:
            barrier.wait(5)
            # Two uploads of the same recording must still produce two jobs.
            started[index] = manager.start(
                Path("natural.wav"),
                Path("prepared"),
                prepare=make_prepare(index),
            )

        threads = [threading.Thread(target=submit, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertTrue(all(entered[index].wait(5) for index in range(2)))
        self.assertNotEqual(started[0]["job_id"], started[1]["job_id"])
        self.assertEqual(len({started[0]["job_id"], started[1]["job_id"]}), 2)
        first_running = manager.snapshot(started[0]["job_id"])
        second_running = manager.snapshot(started[1]["job_id"])
        self.assertEqual(first_running["stage"], "候选 0")
        self.assertEqual(second_running["stage"], "候选 1")

        release.set()
        first = wait_for_terminal(manager, started[0]["job_id"])
        second = wait_for_terminal(manager, started[1]["job_id"])
        self.assertEqual(first["result"]["ref_text"], "文本 0")
        self.assertEqual(second["result"]["ref_text"], "文本 1")
        self.assertEqual(manager.snapshot(started[0]["job_id"])["result"]["ref_audio"], "voice_ref_0.wav")
        self.assertEqual(manager.snapshot(started[1]["job_id"])["result"]["ref_audio"], "voice_ref_1.wav")


class VoicePrepareJobRetentionTests(unittest.TestCase):
    def test_finished_job_history_is_bounded(self) -> None:
        manager = VoicePrepareJobManager(max_retained_jobs=2)
        job_ids = []
        for index in range(5):
            snapshot = manager.start(
                Path(f"natural_{index}.wav"),
                Path("prepared"),
                prepare=instant_prepare(text=f"文本 {index}"),
            )
            job_ids.append(snapshot["job_id"])
            wait_for_terminal(manager, snapshot["job_id"])
        self.assertTrue(wait_until(lambda: manager.job_count <= 2))
        self.assertIsNone(manager.snapshot(job_ids[0]))
        self.assertIsNotNone(manager.snapshot(job_ids[-1]))
        self.assertLessEqual(manager.job_count, 2)

    def test_expired_finished_jobs_are_pruned_but_running_jobs_stay(self) -> None:
        manager = VoicePrepareJobManager(max_retained_jobs=MAX_RETAINED_JOBS, ttl_seconds=0.01)
        release = threading.Event()
        entered = threading.Event()

        def blocking_prepare(source, output_dir, *, language="zh", progress=None):
            entered.set()
            release.wait(5)
            return {"ref_audio": "ref.wav", "ref_text": "运行中的任务", "warnings": []}

        running = manager.start(Path("running.wav"), Path("prepared"), prepare=blocking_prepare)
        self.assertTrue(entered.wait(5))
        finished = manager.start(Path("finished.wav"), Path("prepared"), prepare=instant_prepare())
        wait_for_terminal(manager, finished["job_id"])
        time.sleep(0.03)
        # Starting a new job prunes expired finished jobs, never running ones.
        manager.start(Path("fresh.wav"), Path("prepared"), prepare=instant_prepare(text="新任务"))
        self.assertIsNone(manager.snapshot(finished["job_id"]))
        self.assertIsNotNone(manager.snapshot(running["job_id"]))
        release.set()
        self.assertEqual(wait_for_terminal(manager, running["job_id"])["status"], "done")


class VoiceIngestProgressTests(unittest.TestCase):
    def test_progress_callback_reports_chinese_stages_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "natural.wav"
            sf.write(str(source), speech_like(8.0), 16000)
            events: list[tuple[float, str]] = []
            voice_ingest.prepare_reference_from_audio(
                source,
                root / "prepared",
                transcriber=lambda path: {"text": "今天天气很好。", "language": "zh"},
                judge=lambda path: {
                    "status": "ok",
                    "metrics": {"estimated_snr_db": 30.0, "silence_ratio": 0.1},
                    "warnings": [],
                },
                progress=lambda value, stage: events.append((value, stage)),
            )

        stages = [stage for _value, stage in events]
        values = [value for value, _stage in events]
        self.assertEqual(events[0], (0.05, "读取音频"))
        self.assertEqual(events[1], (0.15, "检测语音"))
        self.assertTrue(any(stage == "逐个候选质检" for stage in stages))
        self.assertTrue(all(0.2 <= value < 0.6 for value, stage in events if stage == "逐个候选质检"))
        self.assertIn((0.7, "写入参考片段"), events)
        self.assertIn((0.85, "语音转写"), events)
        self.assertEqual(events[-1], (1.0, "完成"))
        self.assertEqual(values, sorted(values))
        self.assertTrue(all(0.0 <= value <= 1.0 for value in values))

    def test_default_progress_none_still_returns_the_same_result(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "natural.wav"
            sf.write(str(source), speech_like(6.0), 16000)
            result = voice_ingest.prepare_reference_from_audio(
                source,
                root / "prepared",
                transcriber=lambda path: {"text": "默认行为不变。", "language": "zh"},
                judge=lambda path: {"status": "ok", "metrics": {}, "warnings": []},
            )
            self.assertTrue(Path(result["ref_audio"]).is_file())
        self.assertEqual(result["ref_text"], "默认行为不变。")

    def test_broken_progress_callback_does_not_break_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "natural.wav"
            sf.write(str(source), speech_like(6.0), 16000)

            def broken_progress(value: float, stage: str) -> None:
                raise RuntimeError("进度回调坏了")

            result = voice_ingest.prepare_reference_from_audio(
                source,
                root / "prepared",
                transcriber=lambda path: {"text": "回调失败不影响任务。", "language": "zh"},
                judge=lambda path: {"status": "ok", "metrics": {}, "warnings": []},
                progress=broken_progress,
            )
        self.assertEqual(result["ref_text"], "回调失败不影响任务。")


class VoicePrepareRouteTests(unittest.TestCase):
    @staticmethod
    def _fake_prepare(source, output_dir, *, language="zh", progress=None):
        if progress is not None:
            progress(0.45, "逐个候选质检")
        return {
            "source_path": str(source),
            "ref_audio": str(Path(output_dir) / "voice_ref_route.wav"),
            "ref_text": "今天天气很好。",
            "duration_seconds": 5.0,
            "selected": {"start": 0.0, "end": 5.0, "duration": 5.0},
            "quality": {"status": "ok", "purpose": "synthesis"},
            "warnings": [],
        }

    def _wait_for_route_job(self, client, job_id: str, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = client.get(f"/api/reference-audio/prepare/{job_id}")
            self.assertEqual(response.status_code, 200)
            snapshot = response.json()
            if snapshot["status"] in TERMINAL_STATUSES:
                return snapshot
            time.sleep(0.02)
        self.fail(f"路由任务 {job_id} 没有在 {timeout} 秒内结束")

    def test_prepare_route_returns_a_job_id_and_status_route_returns_the_result(self) -> None:
        from fastapi.testclient import TestClient

        from backend.app import create_app
        from backend.services import core

        with tempfile.TemporaryDirectory() as raw:
            with patch.object(core, "REFERENCE_AUDIO_DIR", Path(raw)), patch.object(
                voice_ingest, "prepare_reference_from_audio", side_effect=self._fake_prepare
            ):
                client = TestClient(create_app())
                response = client.post(
                    "/api/reference-audio/prepare",
                    data={"purpose": "synthesis", "auto_prepare": "true", "language": "zh"},
                    files={"audio": ("natural.wav", b"audio", "audio/wav")},
                )
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertTrue(payload["job_id"])
                self.assertIn(payload["status"], {"queued", "running", "done"})
                snapshot = self._wait_for_route_job(client, payload["job_id"])
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["progress"], 1.0)
        self.assertEqual(snapshot["result"]["ref_text"], "今天天气很好。")
        self.assertEqual(snapshot["error"], "")

    def test_unknown_job_route_returns_404(self) -> None:
        from fastapi.testclient import TestClient

        from backend.app import create_app

        client = TestClient(create_app())
        response = client.get("/api/reference-audio/prepare/voice_prep_missing")
        self.assertEqual(response.status_code, 404)
        self.assertIn("音色准备任务不存在", response.json()["detail"])

    def test_prepare_route_rejects_inspect_only_uploads(self) -> None:
        from fastapi.testclient import TestClient

        from backend.app import create_app
        from backend.services import core

        with tempfile.TemporaryDirectory() as raw:
            with patch.object(core, "REFERENCE_AUDIO_DIR", Path(raw)):
                client = TestClient(create_app())
                response = client.post(
                    "/api/reference-audio/prepare",
                    data={"purpose": "synthesis", "auto_prepare": "false"},
                    files={"audio": ("natural.wav", b"audio", "audio/wav")},
                )
        self.assertEqual(response.status_code, 400)
        self.assertIn("只用于自动准备合成参考音", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
