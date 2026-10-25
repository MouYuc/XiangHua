"""Architecture contracts: GPU work must happen inside a lease.

These encode the answers to the "core architecture self-check": a synthesis run
takes the card before loading models, model loading itself is leased so a warm-up
retry cannot swap weights under a running batch, and both release on failure.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services import core  # noqa: E402
from backend.services.gpu_arbiter import GPU_ARBITER  # noqa: E402


class FakeSynthesisBackend:
    """Records whether the lease was held when the GPU entry points ran."""

    class _InferenceCalls:
        count = 0

    def __init__(self) -> None:
        self._inference_calls = self._InferenceCalls()
        self.ensure_holder: str | None = None
        self.synth_holder: str | None = None

    def ensure(self, cfg: dict) -> None:
        status = GPU_ARBITER.status()
        self.ensure_holder = (status["holder"] or {}).get("kind")

    def synth_segments(self, cfg: dict, segments: list, semantic_token_cb=None):
        status = GPU_ARBITER.status()
        self.synth_holder = (status["holder"] or {}).get("kind")
        return np.zeros(32000, dtype=np.float32), 32000


class _NoOpJudge:
    """Take-selection readback stub: the lease contracts must not load SenseVoice."""

    def cer(self, audio_path, reference_text):
        return None


class GpuLeaseContractTests(unittest.TestCase):
    def setUp(self) -> None:
        GPU_ARBITER.reset_for_tests()
        judge_patch = patch.object(core, "READBACK_JUDGE", _NoOpJudge())
        judge_patch.start()
        self.addCleanup(judge_patch.stop)

    def tearDown(self) -> None:
        GPU_ARBITER.reset_for_tests()

    def test_model_loading_takes_its_own_lease(self) -> None:
        backend = core.SynthesisBackend()
        seen: list[str] = []

        def fake_ensure(cfg: dict) -> None:
            status = GPU_ARBITER.status()
            seen.append((status["holder"] or {}).get("kind"))

        with patch.object(backend, "_ensure_locked", fake_ensure):
            backend.ensure({"gpt_sovits_root": "unused", "gpt_model": "a", "sovits_model": "b"})
        self.assertEqual(seen, ["model_load"])
        self.assertFalse(GPU_ARBITER.status()["busy"])

    def test_synthesis_holds_the_lease_across_ensure_and_inference(self) -> None:
        backend = FakeSynthesisBackend()
        with tempfile.TemporaryDirectory() as raw, patch.object(core, "SYNTHESIS_BACKEND", backend):
            core.synthesize_lines(
                {"text_lang": "zh", "accent_level": "off", "speed": 1.0, "top_k": 5, "top_p": 0.8,
                 "temperature": 0.9, "sample_steps": 8, "pause_second": 0.1, "ref_audio": "x.wav",
                 "ref_text": "", "gpt_sovits_root": str(raw), "gpt_model": "a", "sovits_model": "b"},
                "测试一句。",
                Path(raw) / "out",
            )
        self.assertEqual(backend.ensure_holder, "synthesis")
        self.assertEqual(backend.synth_holder, "synthesis")
        self.assertFalse(GPU_ARBITER.status()["busy"])

    def test_lease_is_released_when_a_line_fails(self) -> None:
        """A per-line engine failure is recorded, and the card must come back."""

        class Exploding(FakeSynthesisBackend):
            def synth_segments(self, cfg, segments, semantic_token_cb=None):
                raise RuntimeError("engine exploded")

        with tempfile.TemporaryDirectory() as raw, patch.object(core, "SYNTHESIS_BACKEND", Exploding()):
            result = core.synthesize_lines(
                {"text_lang": "zh", "accent_level": "off", "speed": 1.0, "top_k": 5, "top_p": 0.8,
                 "temperature": 0.9, "sample_steps": 8, "pause_second": 0.1, "ref_audio": "x.wav",
                 "ref_text": "", "gpt_sovits_root": str(raw), "gpt_model": "a", "sovits_model": "b"},
                "会失败。",
                Path(raw) / "out",
            )
        self.assertFalse(GPU_ARBITER.status()["busy"])
        self.assertIn("engine exploded", result["outputs"][0]["error"])

    def test_second_synthesis_waits_instead_of_interleaving(self) -> None:
        """The lease is FIFO: a queued job must not run while one is inside."""
        events: list[str] = []
        first_inside = threading.Event()
        release = threading.Event()

        class Slow(FakeSynthesisBackend):
            def __init__(self, name: str) -> None:
                super().__init__()
                self.name = name

            def synth_segments(self, cfg, segments, semantic_token_cb=None):
                events.append(f"{self.name}-enter")
                if self.name == "A":
                    first_inside.set()
                    release.wait(5)
                events.append(f"{self.name}-exit")
                return np.zeros(32000, dtype=np.float32), 32000

        cfg = {"text_lang": "zh", "accent_level": "off", "speed": 1.0, "top_k": 5, "top_p": 0.8,
               "temperature": 0.9, "sample_steps": 8, "pause_second": 0.1, "ref_audio": "x.wav",
               "ref_text": "", "gpt_sovits_root": "unused", "gpt_model": "a", "sovits_model": "b"}

        with tempfile.TemporaryDirectory() as raw:
            cfg["gpt_sovits_root"] = raw
            outputs = Path(raw) / "out"
            with patch.object(core, "SYNTHESIS_BACKEND", Slow("A")):
                one = threading.Thread(target=lambda: core.synthesize_lines(cfg, "第一句。", outputs))
                one.start()
                first_inside.wait(5)
            with patch.object(core, "SYNTHESIS_BACKEND", Slow("B")):
                two = threading.Thread(target=lambda: core.synthesize_lines(cfg, "第二句。", outputs))
                two.start()
                two.join(0.3)
                self.assertEqual(events, ["A-enter"])
                release.set()
                one.join(5)
                two.join(5)
        self.assertEqual(events, ["A-enter", "A-exit", "B-enter", "B-exit"])


if __name__ == "__main__":
    unittest.main()
