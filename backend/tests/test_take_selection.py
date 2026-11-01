"""Take-selection (readback retry) contract tests.

Everything here mocks the engine and the readback judge: no model loads, no GPU.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services import core


def _cfg(**overrides):
    base = {
        "gpt_model": "gpt.ckpt",
        "sovits_model": "sovits.pth",
        "ref_audio": "ref.wav",
        "ref_text": "参考。",
        "text_lang": "zh",
        "prompt_lang": "zh",
        "accent_level": "off",
        "auto_lightmid": False,
        "speed": 1.0,
        "temperature": 0.95,
        "top_k": 30,
        "top_p": 0.88,
        "sample_steps": 64,
        "pause_second": 0.15,
        "seed": 20261017,
        "take_selection": True,
    }
    base.update(overrides)
    return base


def _audio(seconds: float, marker: float) -> np.ndarray:
    # Non-silent so the readback path (which in production writes a real wav)
    # exercises writing; marker distinguishes takes in assertions via amplitude.
    return np.full(int(seconds * 32000), marker, dtype=np.float32) * 0.05


class _FakeJudge:
    def __init__(self, cers):
        self.cers = list(cers)
        self.calls = 0

    def cer(self, audio_path, reference_text):
        value = self.cers[min(self.calls, len(self.cers) - 1)]
        self.calls += 1
        return value


def _run(cfg, takes, judge, text="今天天气真不错。"):
    """Run one line through synthesize_lines with a mocked engine."""
    calls = []

    def fake_synth_segments(take_cfg, segments, semantic_token_cb=None):
        calls.append(dict(take_cfg))
        take = len(calls) - 1
        audio, rate = takes[min(take, len(takes) - 1)]
        return audio, rate

    backend = SimpleNamespace(
        ensure=lambda config: None,
        synth_segments=fake_synth_segments,
        _inference_calls=SimpleNamespace(count=1),
    )
    with TemporaryDirectory() as tmp:
        with patch.object(core, "SYNTHESIS_BACKEND", backend), patch.object(
            core, "READBACK_JUDGE", judge
        ), patch.object(core, "append_history", lambda entries: None):
            result = core.synthesize_lines(cfg, text, output_dir=Path(tmp))
    output = result["outputs"][0]
    return output, calls


class TakeSelectionTests(unittest.TestCase):
    def test_good_first_take_is_delivered_without_retry(self) -> None:
        judge = _FakeJudge([0.02])
        output, calls = _run(_cfg(), [(_audio(3.0, 1.0), 32000)], judge)
        self.assertEqual(len(calls), 1)
        self.assertEqual(output["takes"], 1)
        self.assertEqual(output["selected_take"], 0)
        self.assertEqual(output["take_cers"], [0.02])
        self.assertFalse(output["suspiciously_short"])

    def test_catastrophic_first_take_is_rescued_by_second_seed(self) -> None:
        judge = _FakeJudge([0.30, 0.01])
        output, calls = _run(
            _cfg(), [(_audio(3.0, 1.0), 32000), (_audio(3.1, 2.0), 32000)], judge
        )
        self.assertEqual(len(calls), 2)
        # Take 1 must run with a derived seed, not the caller's original.
        self.assertEqual(calls[0]["seed"], 20261017)
        self.assertEqual(
            calls[1]["seed"],
            (20261017 + core._TAKE_SEED_STRIDE) & 0xFFFFFFFF,
        )
        self.assertEqual(output["selected_take"], 1)
        self.assertEqual(output["takes"], 2)

    def test_all_takes_bad_picks_best_readback(self) -> None:
        judge = _FakeJudge([0.5, 0.2, 0.4])
        output, calls = _run(
            _cfg(),
            [
                (_audio(3.0, 1.0), 32000),
                (_audio(3.0, 2.0), 32000),
                (_audio(3.0, 3.0), 32000),
            ],
            judge,
        )
        self.assertEqual(len(calls), core._TAKE_SELECTION_MAX_TAKES)
        self.assertEqual(output["selected_take"], 1)  # best CER 0.2
        self.assertEqual(output["take_cers"], [0.5, 0.2, 0.4])

    def test_short_take_is_retried_even_without_readback_opinion(self) -> None:
        judge = _FakeJudge([None, 0.02])
        output, calls = _run(
            _cfg(), [(_audio(0.2, 1.0), 32000), (_audio(3.0, 2.0), 32000)], judge
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(output["selected_take"], 1)
        self.assertFalse(output["suspiciously_short"])

    def test_unavailable_judge_keeps_first_take(self) -> None:
        judge = _FakeJudge([None])
        output, calls = _run(_cfg(), [(_audio(3.0, 1.0), 32000)], judge)
        self.assertEqual(len(calls), 1)
        self.assertEqual(output["selected_take"], 0)

    def test_take_selection_can_be_disabled(self) -> None:
        judge = _FakeJudge([0.9])
        output, calls = _run(_cfg(take_selection=False), [(_audio(3.0, 1.0), 32000)], judge)
        self.assertEqual(len(calls), 1)
        self.assertEqual(judge.calls, 0)  # not even judged
        self.assertEqual(output["takes"], 1)

    def test_non_zh_language_skips_take_selection(self) -> None:
        judge = _FakeJudge([0.9])
        output, calls = _run(
            _cfg(text_lang="en"), [(_audio(3.0, 1.0), 32000)], judge, text="hello world"
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(judge.calls, 0)

    def test_explicit_mixed_language_skips_take_selection(self) -> None:
        judge = _FakeJudge([0.9])
        backend_calls = []

        def fake_synth(take_cfg, segments, semantic_token_cb=None):
            backend_calls.append(dict(take_cfg))
            return _audio(3.0, 1.0), 32000

        backend = SimpleNamespace(
            ensure=lambda config: None,
            synth_segments=fake_synth,
            _inference_calls=SimpleNamespace(count=1),
        )
        with TemporaryDirectory() as tmp:
            with patch.object(core, "SYNTHESIS_BACKEND", backend), patch.object(
                core, "READBACK_JUDGE", judge
            ), patch.object(core, "append_history", lambda entries: None):
                result = core.synthesize_lines(
                    _cfg(), "<zh>你好</zh><en>hello</en>", output_dir=Path(tmp)
                )
        self.assertEqual(len(backend_calls), 1)
        self.assertEqual(judge.calls, 0)
        self.assertEqual(result["outputs"][0]["takes"], 1)

    def test_take_seeds_are_deterministic_per_line_seed(self) -> None:
        # Same cfg → same derived take-1 seed, so retries stay reproducible.
        judge = _FakeJudge([0.9, 0.9, 0.9])
        takes = [(_audio(3.0, 1.0), 32000)] * 3
        _, calls_a = _run(_cfg(), takes, judge)
        judge2 = _FakeJudge([0.9, 0.9, 0.9])
        _, calls_b = _run(_cfg(), takes, judge2)
        self.assertEqual(
            [call["seed"] for call in calls_a], [call["seed"] for call in calls_b]
        )


if __name__ == "__main__":
    unittest.main()
