"""Timbre job lifecycle: conversion, profile build, cancellation and failures.

``test_timbre_service.py`` covers the pure helpers (corpus digests, reference
selection, upload placement). This file covers the parts that only run on a
worker thread, which is where cancellation was added in 2026-11 — and where a
status that is never written leaves the UI spinning forever.

No model is loaded: ``TimbreBackend.model`` and the kNN-VC runtime are patched.
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

from backend.services import timbre, timbre_features


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for the job to settle")


class ConversionJobTests(unittest.TestCase):
    def make_backend(self) -> timbre.TimbreBackend:
        backend = timbre.TimbreBackend()
        self.addCleanup(backend._release_locked)
        return backend

    def test_a_successful_conversion_reaches_done_with_the_result(self) -> None:
        backend = self.make_backend()
        with patch.object(
            backend,
            "convert",
            return_value={"file": "out.wav", "file_name": "out.wav", "duration": 1.0},
        ) as convert:
            snapshot = backend.start_conversion(Path("in.wav"), "current", {})
            job_id = snapshot["job_id"]
            _wait_for(lambda: backend.snapshot(job_id)["status"] == "done")

        final = backend.snapshot(job_id)
        self.assertEqual(final["status"], "done")
        self.assertEqual(final["result"]["file_name"], "out.wav")
        self.assertIsNotNone(convert.call_args.kwargs["should_stop"])

    def test_a_failing_conversion_is_reported_not_swallowed(self) -> None:
        backend = self.make_backend()
        with patch.object(backend, "convert", side_effect=RuntimeError("boom")):
            job_id = backend.start_conversion(Path("in.wav"), "current", {})["job_id"]
            _wait_for(lambda: backend.snapshot(job_id)["status"] == "failed")

        final = backend.snapshot(job_id)
        self.assertEqual(final["status"], "failed")
        self.assertIn("boom", final["error"])

    def test_a_cancelled_conversion_ends_cancelled_not_failed(self) -> None:
        """Cancellation is a user action, so it must not surface as an error."""
        backend = self.make_backend()
        started = threading.Event()

        def slow_convert(*_args, **_kwargs):
            started.set()
            time.sleep(0.3)
            raise timbre.TimbreCancelled("音色转换已取消。")

        with patch.object(backend, "convert", side_effect=slow_convert):
            job_id = backend.start_conversion(Path("in.wav"), "current", {})["job_id"]
            started.wait(2)
            self.assertTrue(backend.cancel(job_id)["cancelled"])
            _wait_for(lambda: backend.snapshot(job_id)["status"] in {"cancelled", "failed"})

        final = backend.snapshot(job_id)
        self.assertEqual(final["status"], "cancelled")
        self.assertEqual(final["error"], "")
        self.assertEqual(final["message"], "任务已取消。")

    def test_cancelling_before_the_worker_starts_skips_the_conversion(self) -> None:
        backend = self.make_backend()
        gate = threading.Event()

        def gated_convert(*_args, **_kwargs):
            gate.wait(2)
            return {"file": "out.wav"}

        with patch.object(backend, "convert", side_effect=gated_convert) as convert:
            job_id = backend.start_conversion(Path("in.wav"), "current", {})["job_id"]
            backend.cancel(job_id)
            gate.set()
            _wait_for(lambda: backend.snapshot(job_id)["status"] in {"cancelled", "done"})

        # Either it never ran, or it ran and the event was already set: both are
        # acceptable, but it must never end as "failed".
        self.assertIn(backend.snapshot(job_id)["status"], {"cancelled", "done"})
        self.assertLessEqual(convert.call_count, 1)


class ProfileBuildTests(unittest.TestCase):
    def make_backend(self) -> timbre.TimbreBackend:
        backend = timbre.TimbreBackend()
        self.addCleanup(backend._release_locked)
        return backend

    def test_a_profile_build_reaches_done(self) -> None:
        backend = self.make_backend()
        with patch.object(backend, "build_profile", return_value={"profile_id": "current"}):
            job_id = backend.start_profile_build("current", {})["job_id"]
            _wait_for(lambda: backend.snapshot(job_id)["status"] == "done")
        self.assertEqual(backend.snapshot(job_id)["result"]["profile_id"], "current")

    def test_a_profile_build_cancelled_midway_ends_cancelled(self) -> None:
        backend = self.make_backend()
        started = threading.Event()

        def slow_build(*_args, **_kwargs):
            started.set()
            time.sleep(0.3)
            raise timbre.TimbreCancelled("音色转换已取消。")

        with patch.object(backend, "build_profile", side_effect=slow_build):
            job_id = backend.start_profile_build("current", {})["job_id"]
            started.wait(2)
            backend.cancel(job_id)
            _wait_for(lambda: backend.snapshot(job_id)["status"] in {"cancelled", "failed"})
        self.assertEqual(backend.snapshot(job_id)["status"], "cancelled")


class ConversionValidationTests(unittest.TestCase):
    """These raise before any model is touched, so they are cheap to pin."""

    def test_missing_source_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as caught:
            timbre.TimbreBackend().convert(Path("does-not-exist.wav"), "current", {})
        self.assertIn("源音频不存在", str(caught.exception))

    def test_topk_must_be_positive(self) -> None:
        with self.assertRaises(ValueError) as caught:
            timbre.TimbreBackend().convert(Path(__file__), "current", {}, topk=0)
        self.assertIn("topk", str(caught.exception))

    def test_building_a_profile_without_references_is_refused(self) -> None:
        backend = timbre.TimbreBackend()
        # ``reference_files`` moved to ``timbre_refs`` and is called from
        # ``timbre_features``; patch the caller's binding so the refusal fires.
        with patch.object(timbre_features, "reference_files", return_value=[]):
            with self.assertRaises(ValueError) as caught:
                backend.build_profile("current", {})
        self.assertIn("没有可用参考音频", str(caught.exception))


class UnloadTests(unittest.TestCase):
    def test_unload_reports_the_released_state(self) -> None:
        backend = timbre.TimbreBackend()
        backend._model = object()
        report = backend.unload()
        self.assertIn("cuda", report)
        self.assertFalse(report["loaded"])
        self.assertIsNone(backend._model)

    def test_vram_status_survives_a_missing_torch(self) -> None:
        backend = timbre.TimbreBackend()
        with patch.dict(sys.modules, {"torch": None}):
            report = backend.vram_status()
        self.assertIn("cuda", report)


if __name__ == "__main__":
    unittest.main()
