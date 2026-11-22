"""Tests for the read-only job aggregation facade.

Every test injects fake/source-level patches so the suite never depends on a
real training run, a real media session on disk, or the user's current job
stores.
"""

from __future__ import annotations

import contextlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services import core, synthesis_jobs, timbre, voice_prepare_jobs
from backend.services import training as train_backend
from backend.services.job_registry import (
    JOB_REGISTRY,
    JobRegistry,
    normalize_status,
)
from backend.services.synthesis_jobs import SynthesisJobManager
from backend.services.voice_prepare_jobs import VoicePrepareJobManager

JOB_FIELDS = {
    "job_id",
    "kind",
    "title",
    "status",
    "progress",
    "stage",
    "error",
    "started_at",
    "updated_at",
    "cancelable",
    "source",
}

KINDS = {"synthesis", "dialogue", "voice_prepare", "timbre", "training", "media_analysis"}
STATUSES = {"queued", "running", "done", "failed", "cancelled", "unknown"}


class _FakeTimbreBackend:
    """Stands in for ``TimbreBackend`` without touching torch/kNN-VC."""

    def __init__(self, snapshots: list[dict] | None = None) -> None:
        self._snapshots = list(snapshots or [])
        self.list_calls: list[int] = []

    def list_snapshots(self, limit: int = 50) -> list[dict]:
        self.list_calls.append(limit)
        return [dict(row) for row in self._snapshots[: max(0, int(limit))]]


class _FakeMediaService:
    def __init__(self, sessions: list[dict] | None = None, error: Exception | None = None) -> None:
        self.sessions = list(sessions or [])
        self.error = error
        self.list_calls = 0

    def list_sessions(self) -> list[dict]:
        self.list_calls += 1
        if self.error is not None:
            raise self.error
        return [dict(row) for row in self.sessions]


class RegistryTestBase(unittest.TestCase):
    """Build a ``JobRegistry`` whose five sources are fully controlled."""

    def make_registry(
        self,
        *,
        synthesis: list[dict] | None = None,
        synthesis_error: Exception | None = None,
        voice: list[dict] | None = None,
        voice_error: Exception | None = None,
        timbre_backend: object | None = None,
        training_running: list[dict] | None = None,
        training_state: dict | None = None,
        media_sessions: list[dict] | None = None,
        media_error: Exception | None = None,
        media_factory=None,
    ) -> JobRegistry:
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)

        if synthesis_error is not None:
            stack.enter_context(
                patch.object(
                    synthesis_jobs.SYNTHESIS_JOBS,
                    "list_snapshots",
                    side_effect=synthesis_error,
                )
            )
        else:
            stack.enter_context(
                patch.object(
                    synthesis_jobs.SYNTHESIS_JOBS,
                    "list_snapshots",
                    return_value=list(synthesis or []),
                )
            )

        if voice_error is not None:
            stack.enter_context(
                patch.object(
                    voice_prepare_jobs.VOICE_PREPARE_JOBS,
                    "list_snapshots",
                    side_effect=voice_error,
                )
            )
        else:
            stack.enter_context(
                patch.object(
                    voice_prepare_jobs.VOICE_PREPARE_JOBS,
                    "list_snapshots",
                    return_value=list(voice or []),
                )
            )

        stack.enter_context(
            patch.object(
                timbre,
                "TIMBRE_BACKEND",
                timbre_backend if timbre_backend is not None else _FakeTimbreBackend(),
            )
        )
        stack.enter_context(
            patch.object(train_backend, "running_jobs", return_value=list(training_running or []))
        )
        stack.enter_context(
            patch.object(
                train_backend,
                "load_state",
                return_value=training_state if training_state is not None else {"steps": {}},
            )
        )
        stack.enter_context(
            patch.object(
                core,
                "load_config",
                return_value={
                    "gpt_sovits_root": str(
                        Path(tempfile.gettempdir()) / "xianghua_job_registry_test"
                    )
                },
            )
        )

        if media_factory is None:
            service = _FakeMediaService(media_sessions, media_error)
            media_factory = lambda _root, _service=service: _service  # noqa: E731

        return JobRegistry(media_service_factory=media_factory)


class JobRegistryEmptyTests(RegistryTestBase):
    def test_empty_registry_returns_stable_structure(self) -> None:
        registry = self.make_registry()
        payload = registry.list_jobs()

        self.assertEqual(payload["jobs"], [])
        self.assertEqual(payload["errors"], [])
        self.assertEqual(set(payload["counts"]), STATUSES)
        self.assertTrue(all(value == 0 for value in payload["counts"].values()))
        self.assertEqual(set(payload["by_kind"]), KINDS)
        self.assertTrue(all(value == 0 for value in payload["by_kind"].values()))

    def test_module_singleton_is_a_registry(self) -> None:
        self.assertIsInstance(JOB_REGISTRY, JobRegistry)


class JobRegistryListingTests(RegistryTestBase):
    SYNTHESIS_SNAPSHOT = {
        "job_id": "synth_100",
        "status": "running",
        "phase": "prepare",
        "current": 1,
        "total": 12,
        "completed": 1,
        "current_text": "第二句",
        "output_dir": "",
        "outputs": [],
        "error": "",
        "message": "正在合成。",
        "started_at": "2026-10-17 21:00:00",
        "updated_at": "2026-10-17 21:00:03",
    }

    def test_synthesis_job_is_listed_with_every_field(self) -> None:
        registry = self.make_registry(synthesis=[self.SYNTHESIS_SNAPSHOT])
        payload = registry.list_jobs()

        self.assertEqual(len(payload["jobs"]), 1)
        record = payload["jobs"][0]
        self.assertEqual(set(record), JOB_FIELDS)
        self.assertEqual(record["job_id"], "synth_100")
        self.assertEqual(record["kind"], "synthesis")
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["title"], "合成 1/12 句")
        self.assertAlmostEqual(record["progress"], 1 / 12)
        self.assertEqual(record["stage"], "prepare")
        self.assertEqual(record["error"], "")
        self.assertEqual(record["started_at"], "2026-10-17 21:00:00")
        self.assertEqual(record["updated_at"], "2026-10-17 21:00:03")
        # Cancellation landed in 2026-11: a running synthesis can be stopped
        # between lines, so the task centre offers the button.
        self.assertTrue(record["cancelable"])
        self.assertIn("synthesis_jobs", record["source"])
        self.assertEqual(payload["counts"]["running"], 1)
        self.assertEqual(payload["by_kind"]["synthesis"], 1)

    def test_voice_prepare_job_is_listed_with_chinese_stage(self) -> None:
        snapshot = {
            "job_id": "voice_prep_200",
            "status": "running",
            "phase": "prepare",
            "progress": 0.35,
            "stage": "逐个候选质检",
            "result": None,
            "error": "",
            "message": "逐个候选质检…",
            "source_path": "natural.wav",
            "started_at": "2026-10-17 21:10:00",
            "updated_at": "2026-10-17 21:10:04",
        }
        registry = self.make_registry(voice=[snapshot])
        record = registry.list_jobs()["jobs"][0]

        self.assertEqual(record["job_id"], "voice_prep_200")
        self.assertEqual(record["kind"], "voice_prepare")
        self.assertEqual(record["status"], "running")
        self.assertIn("natural.wav", record["title"])
        self.assertEqual(record["stage"], "逐个候选质检")
        self.assertAlmostEqual(record["progress"], 0.35)
        self.assertFalse(record["cancelable"])

    def test_timbre_job_uses_public_snapshot_method(self) -> None:
        backend = _FakeTimbreBackend(
            [
                {
                    "job_id": "timbre_300",
                    "kind": "convert",
                    "status": "running",
                    "phase": "encode",
                    "progress": 0.25,
                    "message": "正在编码源音频。",
                    "error": "",
                    "source_name": "song.wav",
                    "started_at": "2026-10-17 21:20:00",
                    "updated_at": "2026-10-17 21:20:05",
                }
            ]
        )
        registry = self.make_registry(timbre_backend=backend)
        record = registry.list_jobs()["jobs"][0]

        self.assertTrue(backend.list_calls)
        self.assertEqual(record["kind"], "timbre")
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["title"], "音色转换 song.wav")
        self.assertEqual(record["stage"], "encode")
        self.assertAlmostEqual(record["progress"], 0.25)
        self.assertTrue(record["cancelable"])

    def test_timbre_legacy_backend_falls_back_to_internal_jobs(self) -> None:
        """Transitional path: a backend without any public snapshot method."""
        legacy = SimpleNamespace()
        legacy._jobs = {
            "timbre_legacy": SimpleNamespace(
                snapshot={
                    "job_id": "timbre_legacy",
                    "kind": "profile",
                    "status": "done",
                    "phase": "done",
                    "progress": 1.0,
                    "profile_id": "voice_a",
                    "error": "",
                    "started_at": "2026-10-17 20:00:00",
                }
            )
        }
        registry = self.make_registry(timbre_backend=legacy)
        record = registry.list_jobs()["jobs"][0]

        self.assertEqual(record["job_id"], "timbre_legacy")
        self.assertEqual(record["kind"], "timbre")
        self.assertEqual(record["status"], "done")
        self.assertEqual(record["title"], "音色匹配池 voice_a")
        self.assertAlmostEqual(record["progress"], 1.0)


class JobRegistryFailureTests(RegistryTestBase):
    def test_one_broken_source_is_reported_without_breaking_the_listing(self) -> None:
        voice = [
            {
                "job_id": "voice_prep_ok",
                "status": "running",
                "stage": "语音转写",
                "progress": 0.85,
                "source_path": "ok.wav",
                "started_at": "2026-10-17 21:30:00",
                "updated_at": "2026-10-17 21:30:01",
            }
        ]
        registry = self.make_registry(
            synthesis_error=RuntimeError("boom"),
            voice=voice,
        )
        payload = registry.list_jobs()

        self.assertEqual([job["job_id"] for job in payload["jobs"]], ["voice_prep_ok"])
        self.assertEqual(len(payload["errors"]), 1)
        self.assertIn("synthesis", payload["errors"][0])
        self.assertIn("RuntimeError", payload["errors"][0])
        self.assertIn("boom", payload["errors"][0])

    def test_get_unknown_job_id_returns_none(self) -> None:
        registry = self.make_registry()
        self.assertIsNone(registry.get("missing_job"))
        self.assertIsNone(registry.get(""))
        self.assertIsNone(registry.get(None))  # type: ignore[arg-type]

    def test_media_source_failure_is_isolated(self) -> None:
        registry = self.make_registry(media_error=OSError("media root gone"))
        payload = registry.list_jobs()

        self.assertEqual(payload["jobs"], [])
        self.assertEqual(len(payload["errors"]), 1)
        self.assertIn("media_analysis", payload["errors"][0])
        self.assertIn("media root gone", payload["errors"][0])


class JobRegistryTrainingTests(RegistryTestBase):
    def test_running_training_job_is_listed_with_stage_title(self) -> None:
        state = {
            "steps": {
                "train": {
                    "status": "running",
                    "title": "模型训练",
                    "started": "2026-10-17 21:40:00",
                    "ended": "",
                    "progress": {
                        "stage": "s2",
                        "epochs": 12,
                        "overall_percent": 25.0,
                        "global_percent": 25.0,
                    },
                }
            }
        }
        registry = self.make_registry(
            training_running=[
                {
                    "job_id": "train_123",
                    "step": "train",
                    "title": "模型训练",
                    "log": "train_123.log",
                }
            ],
            training_state=state,
        )
        record = registry.list_jobs()["jobs"][0]

        self.assertEqual(record["job_id"], "train_123")
        self.assertEqual(record["kind"], "training")
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["title"], "训练 s2")
        self.assertEqual(record["stage"], "s2")
        self.assertAlmostEqual(record["progress"], 0.25)
        self.assertTrue(record["cancelable"])
        self.assertIn("running_jobs", record["source"])

    def test_finished_training_state_is_listed_but_not_cancelable(self) -> None:
        state = {
            "steps": {
                "train": {
                    "status": "done",
                    "title": "模型训练",
                    "started": "2026-10-17 18:00:00",
                    "ended": "2026-10-17 18:30:00",
                    "progress": {"stage": "s2", "overall_percent": 100.0},
                }
            }
        }
        registry = self.make_registry(training_state=state)
        payload = registry.list_jobs()
        record = payload["jobs"][0]

        self.assertEqual(record["job_id"], "trainstate_train")
        self.assertEqual(record["status"], "done")
        self.assertEqual(record["title"], "训练 s2")
        self.assertAlmostEqual(record["progress"], 1.0)
        self.assertFalse(record["cancelable"])
        self.assertEqual(payload["by_kind"]["training"], 1)


class JobRegistryCancelTests(RegistryTestBase):
    TRAINING_STATE = {
        "steps": {
            "train": {
                "status": "running",
                "title": "模型训练",
                "started": "2026-10-17 21:40:00",
                "ended": "",
                "note": "",
                "progress": {
                    "stage": "s2",
                    "epochs": 12,
                    "overall_percent": 25.0,
                    "global_percent": 25.0,
                },
            }
        }
    }
    TRAINING_RUNNING = [
        {
            "job_id": "train_123",
            "step": "train",
            "title": "模型训练",
            "log": "train_123.log",
        }
    ]

    def test_cancel_training_calls_stop_job(self) -> None:
        registry = self.make_registry(
            training_running=self.TRAINING_RUNNING,
            training_state=self.TRAINING_STATE,
        )
        with patch.object(train_backend, "stop_job", return_value=True) as stop_job:
            result = registry.cancel("train_123")

        self.assertEqual(result, {"cancelled": True, "reason": "已发送终止信号。"})
        stop_job.assert_called_once_with("train_123")

    def test_cancel_synthesis_delegates_to_the_synthesis_manager(self) -> None:
        """Synthesis became cancellable in 2026-11 (cancel event between lines)."""
        registry = self.make_registry(
            synthesis=[JobRegistryListingTests.SYNTHESIS_SNAPSHOT]
        )
        with patch.object(
            synthesis_jobs.SYNTHESIS_JOBS,
            "cancel",
            return_value={"cancelled": True, "reason": "已请求取消。"},
        ) as cancel, patch.object(train_backend, "stop_job") as stop_job:
            result = registry.cancel("synth_100")

        self.assertTrue(result["cancelled"])
        cancel.assert_called_once_with("synth_100")
        stop_job.assert_not_called()

    def test_running_media_analysis_is_listed_as_cancelable(self) -> None:
        """Media analysis became cooperatively cancellable in 2026-11."""
        registry = self.make_registry(
            media_sessions=[{"session_id": "media_1", "job": {"status": "running"}}]
        )
        job = registry.list_jobs()["jobs"][0]
        self.assertTrue(job["cancelable"])

    def test_cancel_media_analysis_delegates_to_the_service(self) -> None:
        """Cancel dispatches to ``cancel_analysis`` (cooperative, per session)."""
        calls: list[str] = []

        def factory(root: str) -> object:
            class _CancellableMediaService(_FakeMediaService):
                def cancel_analysis(self, session_id: str) -> dict:
                    calls.append(session_id)
                    return {"cancelled": True, "reason": "已请求取消。"}

            return _CancellableMediaService(
                [{"session_id": "media_1", "job": {"status": "running"}}]
            )

        registry = self.make_registry(media_factory=factory)
        job = registry.list_jobs()["jobs"][0]
        result = registry.cancel(job["job_id"])

        self.assertEqual(result, {"cancelled": True, "reason": "已请求取消。"})
        self.assertEqual(calls, [job["job_id"]])

    def test_cancel_media_analysis_reports_legacy_service_failure(self) -> None:
        """A service without ``cancel_analysis`` fails gracefully, not fatally."""
        registry = self.make_registry(
            media_sessions=[{"session_id": "media_1", "job": {"status": "running"}}]
        )
        job = registry.list_jobs()["jobs"][0]
        result = registry.cancel(job["job_id"])
        self.assertFalse(result["cancelled"])
        self.assertIn("取消失败", result["reason"])

    def test_cancel_unknown_job_reports_missing(self) -> None:
        registry = self.make_registry()
        self.assertEqual(
            registry.cancel("nope"),
            {"cancelled": False, "reason": "任务不存在。"},
        )


class JobRegistryNormalizationTests(RegistryTestBase):
    def test_normalize_status_handles_each_source_dialect(self) -> None:
        self.assertEqual(normalize_status("idle"), "queued")
        self.assertEqual(normalize_status("pending"), "queued")
        self.assertEqual(normalize_status("running"), "running")
        self.assertEqual(normalize_status("done"), "done")
        self.assertEqual(normalize_status("fail"), "failed")
        self.assertEqual(normalize_status("FAILED"), "failed")
        self.assertEqual(normalize_status("cancelled"), "cancelled")
        self.assertEqual(normalize_status("something-new"), "unknown")
        self.assertEqual(normalize_status(None), "unknown")

    def test_media_session_statuses_are_normalized(self) -> None:
        sessions = [
            {
                "session_id": "media_1",
                "name": "第3集",
                "created_at": "2026-10-17 19:00:00",
                "updated_at": "2026-10-17 19:10:00",
                "job": {
                    "status": "idle",
                    "stage": "等待上传",
                    "progress": 0.0,
                    "message": "",
                    "error": "",
                },
            },
            {
                "session_id": "media_2",
                "name": "第4集",
                "created_at": "2026-10-17 19:05:00",
                "updated_at": "2026-10-17 19:20:00",
                "job": {
                    "status": "fail",
                    "stage": "失败",
                    "progress": 0.0,
                    "message": "分析失败。",
                    "error": "音频损坏",
                },
            },
        ]
        registry = self.make_registry(media_sessions=sessions)
        records = {job["job_id"]: job for job in registry.list_jobs()["jobs"]}

        self.assertEqual(records["media_1"]["status"], "queued")
        self.assertEqual(records["media_1"]["title"], "第3集")
        self.assertEqual(records["media_1"]["kind"], "media_analysis")
        self.assertEqual(records["media_1"]["stage"], "等待上传")
        self.assertEqual(records["media_2"]["status"], "failed")
        self.assertEqual(records["media_2"]["error"], "音频损坏")
        self.assertFalse(records["media_2"]["cancelable"])

    def test_media_service_is_cached_per_root(self) -> None:
        calls: list[str] = []

        def factory(root: str) -> _FakeMediaService:
            calls.append(root)
            return _FakeMediaService([])

        registry = self.make_registry(media_factory=factory)
        registry.list_jobs()
        registry.list_jobs()
        registry.get("missing")

        self.assertEqual(len(calls), 1)


class JobRegistryOrderingTests(RegistryTestBase):
    def test_running_jobs_sort_before_done_jobs(self) -> None:
        done_late = {
            "job_id": "synth_late",
            "status": "done",
            "total": 2,
            "completed": 2,
            "started_at": "2026-10-17 23:00:00",
            "updated_at": "2026-10-17 23:05:00",
        }
        done_early = {
            "job_id": "synth_early",
            "status": "done",
            "total": 2,
            "completed": 2,
            "started_at": "2026-10-17 22:00:00",
            "updated_at": "2026-10-17 22:05:00",
        }
        running = {
            "job_id": "voice_prep_running",
            "status": "running",
            "stage": "逐个候选质检",
            "progress": 0.5,
            "source_path": "slow.wav",
            "started_at": "2026-10-17 20:00:00",
            "updated_at": "2026-10-17 20:00:05",
        }
        registry = self.make_registry(
            synthesis=[done_early, done_late],
            voice=[running],
        )
        jobs = registry.list_jobs()["jobs"]

        self.assertEqual(
            [job["job_id"] for job in jobs],
            ["voice_prep_running", "synth_late", "synth_early"],
        )
        self.assertEqual(jobs[0]["status"], "running")

    def test_limit_truncates_the_merged_list(self) -> None:
        jobs = [
            {
                "job_id": f"synth_{index}",
                "status": "done",
                "total": 1,
                "completed": 1,
                "started_at": f"2026-10-17 22:00:0{index}",
            }
            for index in range(3)
        ]
        registry = self.make_registry(synthesis=jobs)
        payload = registry.list_jobs(limit=2)

        self.assertEqual(len(payload["jobs"]), 2)
        self.assertEqual(payload["counts"]["done"], 2)
        self.assertEqual(registry.list_jobs(limit=0)["jobs"], [])


class SnapshotMethodTests(unittest.TestCase):
    def test_voice_prepare_list_snapshots_returns_copies(self) -> None:
        manager = VoicePrepareJobManager()

        def fake_prepare(source, output_dir, *, language="zh", progress=None):
            return {
                "ref_audio": "ref.wav",
                "ref_text": "原文",
                "duration_seconds": 4.0,
                "warnings": [],
            }

        started = manager.start(Path("natural.wav"), Path("prepared"), prepare=fake_prepare)
        done = manager.wait(started["job_id"], 5)
        self.assertIsNotNone(done)
        rows = manager.list_snapshots(limit=10)

        self.assertEqual([row["job_id"] for row in rows], [started["job_id"]])
        rows[0]["result"]["ref_text"] = "被外部篡改"
        fresh = manager.list_snapshots(limit=10)
        self.assertEqual(fresh[0]["result"]["ref_text"], "原文")
        self.assertEqual(manager.list_snapshots(limit=0), [])

    def test_synthesis_list_snapshots_returns_deep_copies(self) -> None:
        manager = SynthesisJobManager()

        def fake_synthesize(cfg, text, output_dir=None, progress_cb=None, event_cb=None, should_stop=None):
            if progress_cb is not None:
                progress_cb({"status": "running", "completed": 1, "current": 1})
            return {
                "total": 1,
                "completed": 1,
                "output_dir": str(output_dir or "out"),
                "outputs": [{"file": "a.wav"}],
            }

        with patch.object(core, "synthesize_lines", side_effect=fake_synthesize):
            started = manager.start({}, "你好。")
            job_id = started["job_id"]
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                snapshot = manager.snapshot(job_id)
                if snapshot is not None and snapshot["status"] in {"done", "failed"}:
                    break
                time.sleep(0.01)
            rows = manager.list_snapshots(limit=5)

        self.assertEqual([row["job_id"] for row in rows], [job_id])
        self.assertEqual(rows[0]["outputs"][0]["file"], "a.wav")
        rows[0]["outputs"][0]["file"] = "篡改.wav"
        self.assertEqual(manager.list_snapshots(limit=5)[0]["outputs"][0]["file"], "a.wav")
        self.assertEqual(manager.list_snapshots(limit=0), [])


if __name__ == "__main__":
    unittest.main()
