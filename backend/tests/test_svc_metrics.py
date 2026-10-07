"""Unit tests for the singing-conversion metrics.

The metrics exist to answer two questions that must not be conflated: did the melody
survive, and did the timbre change? A converter that copies its input scores perfectly
on the first and fails the second; one that emits target-sounding noise can do the
opposite. The tests therefore check that each half is computed and reported
independently, and that an untrustworthy pitch curve invalidates a comparison instead
of producing a number.

Population-level results - the actual measurements on Vocadito - live in
``docs/SVC_SINGING_EVAL.md``; this file covers the logic.

Run from the repository root:

    .venv\\Scripts\\python.exe -m unittest backend.tests.test_svc_metrics -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import f0_estimate as f0  # noqa: E402
import svc_metrics  # noqa: E402


def track(frequencies: list[float], degenerate: bool = False) -> f0.F0Track:
    values = np.array(frequencies, dtype=np.float64)
    voiced = values > f0.VOICED_FLOOR_HZ
    return f0.F0Track(
        times=np.arange(values.size) * 0.01,
        f0=np.where(voiced, values, 0.0),
        voiced=voiced,
        method="synthetic",
        peak_confidence=0.0 if degenerate else 0.96,
        degenerate=degenerate,
    )


MELODY = [110.0, 123.5, 138.6, 155.6, 174.6, 196.0, 220.0] * 6


class PitchPreservationTests(unittest.TestCase):
    def test_an_identical_curve_scores_perfectly(self) -> None:
        source = track(MELODY)
        result = svc_metrics.pitch_preservation(source, track(MELODY))
        self.assertTrue(result["trustworthy"])
        self.assertEqual(result["median_abs_cents"], 0.0)
        self.assertEqual(result["correlation"], 1.0)
        self.assertEqual(result["vuv_error_rate"], 0.0)

    def test_a_degenerate_source_invalidates_the_comparison(self) -> None:
        """Refusing to score is the point: a number here would be fabricated."""
        result = svc_metrics.pitch_preservation(track(MELODY, degenerate=True), track(MELODY))
        self.assertFalse(result["trustworthy"])
        self.assertIn("源音频", result["reason"])
        self.assertNotIn("median_abs_cents", result)

    def test_a_degenerate_converted_clip_invalidates_the_comparison(self) -> None:
        result = svc_metrics.pitch_preservation(track(MELODY), track(MELODY, degenerate=True))
        self.assertFalse(result["trustworthy"])
        self.assertIn("转换结果", result["reason"])

    def test_an_octave_transposition_is_reported_as_such(self) -> None:
        """This is the real singing failure mode, not a hypothetical one."""
        source = track(MELODY)
        result = svc_metrics.pitch_preservation(source, track([f / 2 for f in MELODY]))
        self.assertTrue(result["trustworthy"])
        self.assertAlmostEqual(result["median_abs_cents"], 1200.0, delta=1.0)
        self.assertAlmostEqual(result["median_transposition_cents"], -1200.0, delta=1.0)
        self.assertEqual(result["raw_chroma_accuracy"], 1.0)
        self.assertEqual(result["raw_pitch_accuracy"], 0.0)

    def test_a_constant_transposition_leaves_the_contour_intact(self) -> None:
        """Correlation and transposition measure different things on purpose."""
        source = track(MELODY)
        result = svc_metrics.pitch_preservation(source, track([f * 1.05 for f in MELODY]))
        self.assertGreater(result["correlation"], 0.99)
        self.assertAlmostEqual(result["median_transposition_cents"], 84.5, delta=1.0)

    def test_inventing_pitch_in_the_gaps_raises_the_voicing_error(self) -> None:
        source = track(MELODY[:20] + [0.0] * 20)
        invented = track(MELODY[:20] + MELODY[:20])
        result = svc_metrics.pitch_preservation(source, invented)
        self.assertGreater(result["vuv_error_rate"], 0.4)

    def test_a_steady_note_has_no_contour_to_correlate(self) -> None:
        """Reported as unmeasurable rather than as zero agreement."""
        result = svc_metrics.log_f0_correlation(track([150.0] * 40), track([150.0] * 40))
        self.assertIsNone(result["correlation"])
        self.assertIn("note", result)


class TargetCheckTests(unittest.TestCase):
    def frame(self, **pitch) -> dict:
        base = {
            "median_abs_cents": 30.0,
            "raw_chroma_accuracy": 0.95,
            "vuv_error_rate": 0.05,
            "correlation": 0.9,
        }
        return {
            "pitch": {**base, **pitch},
            "timbre": {
                "secs_converted_vs_target": 0.75,
                "secs_improvement": 0.3,
            },
        }

    def test_a_good_result_passes_every_target(self) -> None:
        result = svc_metrics.check_targets(self.frame())
        self.assertEqual(result["failed"], [])
        self.assertEqual(len(result["passed"]), 6)

    def test_each_target_can_fail_independently(self) -> None:
        for key, value in (
            ("median_abs_cents", 120.0),
            ("raw_chroma_accuracy", 0.5),
            ("vuv_error_rate", 0.5),
            ("correlation", 0.2),
        ):
            result = svc_metrics.check_targets(self.frame(**{key: value}))
            self.assertIn(
                {
                    "median_abs_cents": "f0_median_abs_cents",
                    "raw_chroma_accuracy": "f0_chroma_accuracy",
                    "vuv_error_rate": "vuv_error_rate",
                    "correlation": "f0_correlation",
                }[key],
                result["failed"],
                key,
            )

    def test_the_timbre_targets_need_a_reference_to_check(self) -> None:
        result = svc_metrics.check_targets({"pitch": self.frame()["pitch"], "timbre": {}})
        self.assertNotIn("secs_to_target", result["passed"])

    def test_moving_the_voice_towards_the_target_is_what_passes(self) -> None:
        """SECS to target alone is not the claim; the improvement over the source is."""
        result = svc_metrics.check_targets(
            {
                "pitch": self.frame()["pitch"],
                "timbre": {"secs_converted_vs_target": 0.75, "secs_improvement": 0.02},
            }
        )
        self.assertIn("secs_margin_over_source", result["failed"])

    def test_an_unmeasurable_metric_fails_rather_than_passing(self) -> None:
        result = svc_metrics.check_targets(self.frame(median_abs_cents=None))
        self.assertIn("f0_median_abs_cents", result["failed"])

    def test_the_targets_are_recorded_as_project_values_not_paper_values(self) -> None:
        """The kNN-VC paper has no singing experiments, so nothing here is quoted."""
        result = svc_metrics.check_targets(self.frame())
        self.assertIn("不是论文数值", result["note"])
        self.assertIn("kNN-VC", result["note"])

    def test_targets_only_cover_metrics_that_are_actually_computed(self) -> None:
        produced = set()
        result = svc_metrics.check_targets(self.frame())
        produced.update(result["passed"])
        produced.update(result["failed"])
        self.assertTrue(produced.issubset(set(svc_metrics.PROJECT_TARGETS)))


class VoicedDecisionTests(unittest.TestCase):
    def test_identical_voicing_scores_zero_error(self) -> None:
        source = track(MELODY[:20] + [0.0] * 20)
        result = svc_metrics.voiced_decision_error(source, track(MELODY[:20] + [0.0] * 20))
        self.assertEqual(result["error_rate"], 0.0)

    def test_completely_inverted_voicing_scores_total_error(self) -> None:
        source = track(MELODY[:20] + [0.0] * 20)
        converted = track([0.0] * 20 + MELODY[:20])
        result = svc_metrics.voiced_decision_error(source, converted)
        self.assertEqual(result["error_rate"], 1.0)

    def test_the_voiced_fractions_are_reported_for_both_sides(self) -> None:
        """A systematic estimator bias cancels, and showing both sides proves it."""
        source = track(MELODY[:10] + [0.0] * 10)
        converted = track(MELODY[:10] + [0.0] * 10)
        result = svc_metrics.voiced_decision_error(source, converted)
        self.assertEqual(result["source_voiced_fraction"], 0.5)
        self.assertEqual(result["converted_voiced_fraction"], 0.5)


class DocumentedCaveatTests(unittest.TestCase):
    def test_the_no_singing_baseline_caveat_is_in_the_module(self) -> None:
        """A future reader must not mistake these thresholds for literature values."""
        source = Path(svc_metrics.__file__).read_text(encoding="utf-8")
        self.assertIn("no singing experiments", source)
        self.assertIn("2305.18975", source)


if __name__ == "__main__":
    unittest.main()