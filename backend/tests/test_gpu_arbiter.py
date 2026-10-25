"""Tests for the process-wide GPU lease."""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services.gpu_arbiter import GpuArbiter, GpuBusyTimeout  # noqa: E402


class GpuArbiterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.arbiter = GpuArbiter(default_timeout=5.0)

    def tearDown(self) -> None:
        self.arbiter.reset_for_tests()

    def test_second_lease_waits_for_the_first(self) -> None:
        order: list[str] = []
        held = threading.Event()
        release = threading.Event()

        def first() -> None:
            with self.arbiter.lease("synthesis", label="第一个"):
                order.append("first-in")
                held.set()
                release.wait(5)
            order.append("first-out")

        def second() -> None:
            held.wait(5)
            with self.arbiter.lease("analysis", label="第二个"):
                order.append("second-in")

        one = threading.Thread(target=first)
        two = threading.Thread(target=second)
        one.start()
        two.start()
        held.wait(5)
        time.sleep(0.1)
        release.set()
        one.join(5)
        two.join(5)
        self.assertEqual(order, ["first-in", "first-out", "second-in"])

    def test_waiters_are_served_in_arrival_order(self) -> None:
        order: list[str] = []
        release = threading.Event()
        queued = threading.Semaphore(0)

        def holder() -> None:
            with self.arbiter.lease("train", label="训练"):
                release.wait(5)

        def waiter(name: str) -> None:
            queued.acquire()
            with self.arbiter.lease("synthesis", label=name):
                order.append(name)

        holding = threading.Thread(target=holder)
        holding.start()
        time.sleep(0.1)
        threads = []
        for name in ("B", "C"):
            thread = threading.Thread(target=waiter, args=(name,))
            thread.start()
            threads.append(thread)
            queued.release()
            time.sleep(0.1)  # let this waiter enqueue before the next one starts
        time.sleep(0.1)
        release.set()
        holding.join(5)
        for thread in threads:
            thread.join(5)
        self.assertEqual(order, ["B", "C"])

    def test_timeout_names_the_current_holder(self) -> None:
        release = threading.Event()

        def holder() -> None:
            with self.arbiter.lease("train", label="训练任务 A"):
                release.wait(5)

        thread = threading.Thread(target=holder)
        thread.start()
        time.sleep(0.1)
        try:
            with self.assertRaises(GpuBusyTimeout) as caught:
                with self.arbiter.lease("synthesis", timeout=0.2):
                    self.fail("lease should not have been granted")
        finally:
            release.set()
            thread.join(5)
        self.assertIn("训练任务 A", str(caught.exception))
        self.assertEqual(caught.exception.holder, "训练任务 A")
        self.assertGreaterEqual(caught.exception.waited_seconds, 0.15)

    def test_reentrant_lease_from_the_same_thread(self) -> None:
        with self.arbiter.lease("synthesis", label="外层"):
            with self.arbiter.lease("synthesis", label="内层", timeout=0.1):
                self.assertTrue(self.arbiter.status()["busy"])
        self.assertFalse(self.arbiter.status()["busy"])

    def test_lease_is_released_when_the_body_raises(self) -> None:
        with self.assertRaises(ValueError):
            with self.arbiter.lease("synthesis", label="会失败"):
                raise ValueError("boom")
        self.assertFalse(self.arbiter.status()["busy"])
        with self.arbiter.lease("synthesis", label="之后", timeout=0.5):
            self.assertTrue(self.arbiter.status()["busy"])

    def test_status_reports_holder_and_queue(self) -> None:
        release = threading.Event()
        ready = threading.Event()

        def holder() -> None:
            with self.arbiter.lease("synthesis", job_id="job_1", label="合成任务"):
                ready.set()
                release.wait(5)

        def waiter() -> None:
            ready.wait(5)
            with self.arbiter.lease("analysis", label="排队中", timeout=5):
                pass

        holding = threading.Thread(target=holder)
        holding.start()
        ready.wait(5)
        waiting = threading.Thread(target=waiter)
        waiting.start()
        deadline = time.time() + 3
        while time.time() < deadline and self.arbiter.status()["queue_length"] == 0:
            time.sleep(0.02)
        status = self.arbiter.status()
        self.assertTrue(status["busy"])
        self.assertEqual(status["holder"]["job_id"], "job_1")
        self.assertEqual(status["holder"]["label"], "合成任务")
        self.assertEqual(status["queue_length"], 1)
        self.assertEqual(status["queue"][0]["label"], "排队中")
        release.set()
        holding.join(5)
        waiting.join(5)
        self.assertFalse(self.arbiter.status()["busy"])

    def test_timeout_none_waits_until_released(self) -> None:
        order: list[str] = []
        ready = threading.Event()

        def holder() -> None:
            with self.arbiter.lease("train", label="长任务"):
                ready.set()
                time.sleep(0.3)
            order.append("released")

        def waiter() -> None:
            ready.wait(5)
            with self.arbiter.lease("synthesis", timeout=None):
                order.append("acquired")

        holding = threading.Thread(target=holder)
        waiting = threading.Thread(target=waiter)
        holding.start()
        waiting.start()
        holding.join(5)
        waiting.join(5)
        self.assertEqual(order, ["released", "acquired"])


if __name__ == "__main__":
    unittest.main()
