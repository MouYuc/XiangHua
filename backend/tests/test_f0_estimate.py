"""Unit tests for pitch estimation and its guards.

These tests are about the arithmetic and the guards, not about the estimator's
accuracy - accuracy is validated against Vocadito's hand annotations by
``scripts/f0_estimate.py --validate`` (median error 8.6 cents, raw pitch accuracy
0.982) and that measurement is recorded in the project history.

Three defects found while building this are pinned here, because each one produced a
plausible-looking number rather than an error:

* an RMSE dominated by octave errors says nothing about the melody, so raw pitch and
  raw chroma accuracy are reported as a pair and their gap *is* the octave rate;
* a pitch estimator that finds no periodicity at all still returns a full curve
  (CREPE reads a median 2005 Hz with peak confidence 0.0166 on the user's own
  recordings), so a degenerate track must be refused rather than scored;
* comparing a converted clip against its source needs both curves to be trustworthy,
  so one bad curve invalidates the comparison instead of yielding a number.

Run from the repository root:

    .venv\\Scripts\\python.exe -m unittest backend.tests.test_f0_estimate -v
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

import f0_estimate as f0


def track(frequencies: list[float], hop: float = 0.01, method: str = "synthetic", degenerate: bool = False) -> f0.F0Track:
    """Build a track from a list of F0 values; 0 means unvoiced."""
    values = np.array(frequencies, dtype=np.float64)
    voiced = values > f0.VOICED_FLOOR_HZ
    peak = 0.0 if degenerate else 0.96
    return f0.F0Track(
        times=np.arange(values.size) * hop,
        f0=np.where(voiced, values, 0.0),
        voiced=voiced,
        method=method,
        peak_confidence=peak,
        degenerate=degenerate,
    )


class CentsMathTests(unittest.TestCase):
    def test_an_octave_is_1200_cents(self) -> None:
        error = f0.cents_error(np.array([220.0]), np.array([110.0]))
        self.assertAlmostEqual(float(error[0]), 1200.0, places=6)

    def test_a_semitone_is_100_cents(self) -> None:
        error = f0.cents_error(np.array([110.0 * 2 ** (1 / 12)]), np.array([110.0]))
        self.assertAlmostEqual(float(error[0]), 100.0, places=4)

    def test_a_perfect_match_is_zero(self) -> None:
        self.assertAlmostEqual(float(f0.cents_error(np.array([150.0]), np.array([150.0]))[0]), 0.0)

    def test_the_sign_follows_the_direction(self) -> None:
        self.assertLess(float(f0.cents_error(np.array([100.0]), np.array([200.0]))[0]), 0.0)
        self.assertGreater(float(f0.cents_error(np.array([200.0]), np.array([100.0]))[0]), 0.0)

    def test_folding_removes_whole_octaves_only(self) -> None:
        arranged = np.array([1200.0, -1200.0, 2400.0, 74.0, -50.0])
        folded = f0.octave_folded(arranged)
        for value in folded[:3]:
            self.assertAlmostEqual(float(value), 0.0, places=6)
        self.assertAlmostEqual(float(folded[3]), 74.0, places=6)
        self.assertAlmostEqual(float(folded[4]), -50.0, places=6)

    def test_folding_reports_the_distance_to_the_nearest_octave(self) -> None:
        """Folding does not "leave small errors alone"; it maps onto one octave.

        A 700-cent error is nearer a fifth than an octave, so folding puts it at -500
        cents - the same pitch class, one octave down - and it stays far outside the
        50-cent band. That is the correct behaviour: folding exists to stop a wrong
        *register* from counting as a wrong *note*, not to forgive a wrong note.
        """
        self.assertAlmostEqual(float(f0.octave_folded(np.array([700.0]))[0]), -500.0, places=6)
        # 610 rounds up to one octave, i.e. it lands at -590: the same pitch class,
        # and the magnitude is the distance to that octave.
        self.assertAlmostEqual(float(f0.octave_folded(np.array([610.0]))[0]), -590.0, places=6)
        self.assertAlmostEqual(float(f0.octave_folded(np.array([590.0]))[0]), 590.0, places=6)
        self.assertAlmostEqual(float(f0.octave_folded(np.array([1200.0]))[0]), 0.0, places=6)
        self.assertAlmostEqual(float(f0.octave_folded(np.array([1300.0]))[0]), 100.0, places=6)

    def test_folding_never_increases_the_error_beyond_half_an_octave(self) -> None:
        """The nearest octave is never further than 600 cents away."""
        errors = np.linspace(-3600.0, 3600.0, 721)
        self.assertLessEqual(float(np.abs(f0.octave_folded(errors)).max()), 600.0)


class CompareTracksTests(unittest.TestCase):
    def test_a_perfect_estimate_scores_perfectly(self) -> None:
        reference = track([110.0] * 20 + [0.0] * 5 + [130.0] * 20)
        scores = f0.compare_tracks(reference, reference)
        self.assertEqual(scores["cents_median_abs"], 0.0)
        self.assertEqual(scores["raw_pitch_accuracy"], 1.0)
        self.assertEqual(scores["octave_error_rate"], 0.0)
        self.assertEqual(scores["voicing_accuracy"], 1.0)
        self.assertEqual(scores["scored_frames"], 40)

    def test_unvoiced_frames_are_not_scored_for_pitch(self) -> None:
        """Only jointly voiced frames carry a pitch comparison."""
        reference = track([110.0] * 10 + [0.0] * 10)
        estimated = track([110.0] * 10 + [220.0] * 10)  # invents pitch in the gap
        scores = f0.compare_tracks(estimated, reference)
        self.assertEqual(scores["scored_frames"], 10)
        self.assertEqual(scores["voiced_false_positive"], 10)
        # 10 frames are correctly voiced and 10 are invented, so half of what the
        # estimator called voiced it made up.
        self.assertEqual(scores["voicing_precision"], 0.5)
        self.assertEqual(scores["voicing_recall"], 1.0)

    def test_an_octave_error_shows_up_in_the_gap_between_the_two_accuracies(self) -> None:
        """The pair exists precisely so this case is visible rather than averaged away."""
        reference = track([110.0] * 40)
        estimated = track([220.0] * 40)  # every frame exactly one octave up
        scores = f0.compare_tracks(estimated, reference)
        self.assertEqual(scores["raw_pitch_accuracy"], 0.0)
        self.assertEqual(scores["raw_chroma_accuracy"], 1.0)
        self.assertEqual(scores["octave_error_frames"], 40)
        self.assertEqual(scores["octave_error_rate"], 1.0)
        self.assertEqual(scores["cents_median_abs"], 1200.0)
        # And this is why RMSE alone is not reported: it is 1200 here while the
        # melody is in fact perfectly tracked.
        self.assertEqual(scores["cents_rmse"], 1200.0)
        self.assertEqual(scores["cents_rmse_octave_folded"], 0.0)

    def test_a_constant_transposition_keeps_full_correlation(self) -> None:
        """Correlation measures shape; the transposition is reported separately."""
        reference = track([100.0, 120.0, 140.0, 160.0, 180.0] * 8)
        shifted = track([v * 2 ** (200 / 1200) for v in [100.0, 120.0, 140.0, 160.0, 180.0] * 8])
        scores = f0.compare_tracks(shifted, reference)
        self.assertAlmostEqual(scores["cents_bias"], 200.0, places=1)
        self.assertEqual(scores["raw_pitch_accuracy"], 0.0)

    def test_no_overlap_gives_a_null_pitch_error(self) -> None:
        reference = track([110.0] * 10 + [0.0] * 10)
        estimated = track([0.0] * 10 + [110.0] * 10)
        scores = f0.compare_tracks(estimated, reference)
        self.assertEqual(scores["scored_frames"], 0)
        self.assertIsNone(scores["cents_median_abs"])
        self.assertIsNone(scores["raw_pitch_accuracy"])
        self.assertIsNone(scores["cents_rmse"])


class DegenerateGuardTests(unittest.TestCase):
    def test_a_degenerate_track_is_reported_untrustworthy(self) -> None:
        bad = track([2005.0] * 20, degenerate=True)
        report = bad.reliability()
        self.assertFalse(report["trustworthy"])
        self.assertIn("不能用于结论", report["reason"])

    def test_a_healthy_track_is_trustworthy_and_reports_its_confidence(self) -> None:
        good = track([110.0] * 20)
        report = good.reliability()
        self.assertTrue(report["trustworthy"])
        self.assertEqual(report["peak_confidence"], 0.96)

    def test_the_threshold_is_higher_than_the_observed_failure_and_below_real_audio(self) -> None:
        """Measured gap: the user's slices peak at 0.0166, real singing at 0.96."""
        self.assertGreater(f0.DEGENERATE_CONFIDENCE, 0.0166)
        self.assertLess(f0.DEGENERATE_CONFIDENCE, 0.9)


class TrackResamplingTests(unittest.TestCase):
    def test_resampling_carries_unvoiced_frames_as_zero(self) -> None:
        source = track([110.0, 110.0, 0.0, 0.0, 110.0])
        values = source.resample_to(np.array([0.0, 0.01, 0.02, 0.03, 0.04]))
        self.assertEqual(values[0], 110.0)
        self.assertEqual(values[2], 0.0)

    def test_resampling_outside_the_range_is_zero_not_extrapolated(self) -> None:
        source = track([110.0, 110.0])
        values = source.resample_to(np.array([-1.0, 5.0]))
        self.assertEqual(list(values), [0.0, 0.0])


class GroundTruthReaderTests(unittest.TestCase):
    def test_the_zero_convention_means_unvoiced(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "clip_f0.csv"
            path.write_text("0.0,0.0\n0.005,110.0\n0.010,0.0\n0.015,220.0\n", encoding="utf-8")
            reference = f0.read_ground_truth(path)
            self.assertEqual(list(reference.voiced), [False, True, False, True])
            self.assertEqual(list(reference.f0), [0.0, 110.0, 0.0, 220.0])
            self.assertAlmostEqual(reference.times[1], 0.005)


if __name__ == "__main__":
    unittest.main()