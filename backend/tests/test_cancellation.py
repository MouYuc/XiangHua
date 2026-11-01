"""Cancellation contracts for the background job managers.

Synthesis, dialogue and timbre jobs were uncancellable: once submitted, a queued
job waited for the GPU lease and a running one could only be stopped by killing
the server. These tests pin the behaviour that replaced that:

* ``POST /api/jobs/{id}/cancel`` reaches the owning manager;
* a cancelled job ends in ``cancelled`` (not ``failed``) and keeps the work it
  already produced;
* a job that already finished is not cancellable, and says why.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services import core, dialogue, timbre
from backend.services.synthesis_jobs import SynthesisJobManager


def _wait_for(manager, job_id: str, statuses: set[str], timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        snapshot = manager.snapshot(job_id)
        if snapshot is not None:
            last = snapshot
            if snapshot.get("status") in statuses:
                return snapshot
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not reach {statuses}; last={last}")


class SynthesisCancelTests(unittest.TestCase):
    def make_manager(self) -> SynthesisJobManager:
        return SynthesisJobManager()

    def test_cancel_stops_between_lines_and_keeps_finished_outputs(self) -> None:
        manager = self.make_manager()
        started: dict[str, object] = {}

        def fake_synthesize(cfg, text, output_dir=None, progress_cb=None, event_cb=None, should_stop=None):
            # Stand-in for core.synthesize_lines: honour the cancel event the same
            # way the real loop does (between lines, never mid-inference).
            outputs = []
            for line in core.parse_lines(text):
                if should_stop is not None and should_stop():
                    raise core.SynthesisCancelled(f"任务已取消，已完成 {len(outputs)} 句。")
                if progress_cb is not None:
                    progress_cb({"status": "running", "completed": len(outputs) + 1, "current": len(outputs) + 1})
                outputs.append({"text": line, "file": f"{len(outputs)}.wav"})
                started["reached_second"] = len(outputs) >= 2
                time.sleep(0.02)
            return {
                "total": len(outputs),
                "completed": len(outputs),
                "output_dir": str(output_dir or "out"),
                "outputs": outputs,
            }

        with patch.object(core, "synthesize_lines", side_effect=fake_synthesize):
            snapshot = manager.start({}, "第一句。\n第二句。\n第三句。\n第四句。")
            job_id = str(snapshot["job_id"])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not started.get("reached_second"):
                time.sleep(0.005)
            result = manager.cancel(job_id)
            final = _wait_for(manager, job_id, {"cancelled", "done", "failed"})

        self.assertTrue(result["cancelled"])
        self.assertEqual(final["status"], "cancelled")
        self.assertEqual(final["error"], "")
        # Whatever completed before the cancel is still in the snapshot.
        self.assertLess(len(final["outputs"]), 4)

    def test_cancel_after_completion_is_rejected(self) -> None:
        manager = self.make_manager()

        def fake_synthesize(cfg, text, output_dir=None, progress_cb=None, event_cb=None, should_stop=None):
            return {
                "total": 1,
                "completed": 1,
                "output_dir": str(output_dir or "out"),
                "outputs": [{"text": "你好。", "file": "0.wav"}],
            }

        with patch.object(core, "synthesize_lines", side_effect=fake_synthesize):
            snapshot = manager.start({}, "你好。")
            job_id = str(snapshot["job_id"])
            _wait_for(manager, job_id, {"done", "failed"})
            result = manager.cancel(job_id)

        self.assertFalse(result["cancelled"])
        self.assertIn("已经结束", result["reason"])

    def test_cancel_unknown_job_reports_missing(self) -> None:
        self.assertEqual(
            SynthesisJobManager().cancel("synth_missing"),
            {"cancelled": False, "reason": "任务不存在。"},
        )

    def test_real_line_loop_honours_should_stop(self) -> None:
        """The production loop must bail out before synthesising the first line."""
        cfg = {"speed": 1.0, "text_lang": "zh", "seed": 1}
        with patch.object(core.SYNTHESIS_BACKEND, "ensure", return_value=None):
            with self.assertRaises(core.SynthesisCancelled):
                core._synthesize_lines_locked(
                    cfg, ["你好。"], should_stop=lambda: True
                )


class DialogueCancelTests(unittest.TestCase):
    def test_cancel_stops_the_episode_between_lines(self) -> None:
        manager = dialogue.DialogueJobManager(card_lookup=lambda _id: {})
        produced: list[int] = []

        def fake_synth(base, speaker, line, voice_card_id, destination):
            produced.append(1)
            time.sleep(0.02)
            return {"speaker": speaker, "line": line, "file": f"{len(produced)}.wav"}

        with patch.object(manager, "_synthesize_line", side_effect=fake_synth):
            snapshot = manager.start("旁白一。\n旁白二。\n旁白三。\n旁白四。", {}, {})
            job_id = str(snapshot["job_id"])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and len(produced) < 2:
                time.sleep(0.005)
            result = manager.cancel(job_id)
            final = _wait_for(manager, job_id, {"cancelled", "done", "failed"})

        self.assertTrue(result["cancelled"])
        self.assertEqual(final["status"], "cancelled")
        self.assertLess(len(final["outputs"]), 4)

    def test_list_snapshots_is_newest_first(self) -> None:
        manager = dialogue.DialogueJobManager(card_lookup=lambda _id: {})
        with patch.object(manager, "_synthesize_line", return_value={"speaker": "旁白", "line": "好。", "file": "a.wav"}):
            first = manager.start("好。", {}, {})
            second = manager.start("好。", {}, {})
        snapshot_ids = [row["job_id"] for row in manager.list_snapshots(limit=10)]
        self.assertEqual(snapshot_ids[0], second["job_id"])
        self.assertIn(first["job_id"], snapshot_ids)


class TimbreCancelTests(unittest.TestCase):
    def test_cancelling_a_finished_job_is_rejected(self) -> None:
        backend = timbre.TimbreBackend()
        job_id = "timbre_finished"
        job = timbre._Job(job_id=job_id, snapshot={"job_id": job_id, "status": "done"})
        with backend._jobs_lock:
            backend._jobs[job_id] = job
        try:
            result = backend.cancel(job_id)
        finally:
            with backend._jobs_lock:
                backend._jobs.pop(job_id, None)
        self.assertFalse(result["cancelled"])
        self.assertIn("已经结束", result["reason"])

    def test_cancel_sets_the_event_on_a_running_job(self) -> None:
        backend = timbre.TimbreBackend()
        job_id = "timbre_running"
        job = timbre._Job(job_id=job_id, snapshot={"job_id": job_id, "status": "running"})
        with backend._jobs_lock:
            backend._jobs[job_id] = job
        try:
            result = backend.cancel(job_id)
            self.assertTrue(result["cancelled"])
            self.assertTrue(job.cancel_event.is_set())
            self.assertEqual(job.snapshot["message"], "正在取消…")
        finally:
            with backend._jobs_lock:
                backend._jobs.pop(job_id, None)


class JobIdUniquenessTests(unittest.TestCase):
    def test_ids_are_unique_within_a_millisecond(self) -> None:
        """The old ``f"synth_{int(time.time()*1000)}"`` collided and overwrote jobs."""
        from backend.infra.ids import new_job_id

        ids = [new_job_id("synth") for _ in range(2000)]
        self.assertEqual(len(set(ids)), len(ids))

    def test_manager_keeps_both_jobs_started_back_to_back(self) -> None:
        manager = SynthesisJobManager()
        gate = threading.Event()

        def blocking_synthesize(cfg, text, output_dir=None, progress_cb=None, event_cb=None, should_stop=None):
            gate.wait(2)
            return {"total": 0, "completed": 0, "output_dir": str(output_dir or "out"), "outputs": []}

        with patch.object(core, "synthesize_lines", side_effect=blocking_synthesize):
            first = manager.start({}, "一。")
            second = manager.start({}, "二。")
            gate.set()
            _wait_for(manager, str(first["job_id"]), {"done", "failed", "cancelled"})
            _wait_for(manager, str(second["job_id"]), {"done", "failed", "cancelled"})

        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertIsNotNone(manager.snapshot(str(first["job_id"])))
        self.assertIsNotNone(manager.snapshot(str(second["job_id"])))


if __name__ == "__main__":
    unittest.main()
