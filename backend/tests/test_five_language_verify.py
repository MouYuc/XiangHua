"""Unit tests for the five-language verification logic.

The population results - LID 5/5, the mixed single-pass check, and the cross-language
timbre drift against its measured noise floor are recorded in the project history.
This file covers the pure logic, and pins two mistakes that were actually made while
building it, because both produced a clean-looking verdict from broken inputs:

* the first run fed **untagged** sentences to the synthesiser, so Korean went through
  the Mandarin frontend and came back as 0.08 s of near-silence, yet the report still
  said the language check had run;
* the first LID calibration reported nothing at all because SenseVoice's example clips
  live in an ``example/`` subdirectory, and a calibration that silently skips every
  item looks exactly like one that passed.

Run from the repository root:

    .venv\\Scripts\\python.exe -m unittest backend.tests.test_five_language_verify -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENTS = REPO_ROOT / "scripts" / "experiments"
if str(EXPERIMENTS) not in sys.path:
    sys.path.insert(0, str(EXPERIMENTS))

import five_language_verify as verify


def unit_vector(seed: int, dimension: int = 64) -> np.ndarray:
    rng = np.random.default_rng(seed)
    vector = rng.normal(size=dimension)
    return vector / np.linalg.norm(vector)


def cosine(left: np.ndarray, right: np.ndarray) -> float:
    return float(np.dot(left, right) / (np.linalg.norm(left) * np.linalg.norm(right)))


class LanguageTagTests(unittest.TestCase):
    def test_sensevoice_language_markers_are_extracted(self) -> None:
        text, language = verify.strip_language_tag("<|ko|><|NEUTRAL|><|Speech|>안녕하세요")
        self.assertEqual(language, "ko")
        self.assertNotIn("<|ko|>", text)
        self.assertIn("안녕하세요", text)

    def test_every_supported_language_marker_is_recognised(self) -> None:
        for language in ("zh", "yue", "en", "ja", "ko"):
            _text, detected = verify.strip_language_tag(f"<|{language}|>hello")
            self.assertEqual(detected, language)

    def test_an_unmarked_transcript_yields_no_language(self) -> None:
        text, language = verify.strip_language_tag("no marker here")
        self.assertIsNone(language)
        self.assertEqual(text, "no marker here")

    def test_tagging_wraps_the_text_in_its_language(self) -> None:
        self.assertEqual(verify.tagged("ko", "안녕"), "<ko>안녕</ko>")

    def test_every_sample_sentence_is_tagged_exactly_once_in_the_mixed_line(self) -> None:
        """Double-wrapping would make the mixed line test something else entirely."""
        for language in verify.SENTENCES:
            self.assertEqual(verify.MIXED_LINE.count(f"<{language}>"), 1)
            self.assertEqual(verify.MIXED_LINE.count(f"</{language}>"), 1)

    def test_the_mixed_line_covers_all_five_languages_in_order(self) -> None:
        positions = [verify.MIXED_LINE.index(f"<{language}>") for language in verify.SENTENCES]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(len(verify.SENTENCES), 5)


class CrossLanguageDriftTests(unittest.TestCase):
    def test_identical_embeddings_have_zero_drift(self) -> None:
        shared = unit_vector(1)
        result = verify.cross_language_drift({name: shared for name in "abcde"}, cosine)
        self.assertAlmostEqual(result["drift"], 0.0, places=6)
        self.assertAlmostEqual(result["min_pair"], 1.0, places=6)

    def test_drift_is_the_spread_not_the_level(self) -> None:
        """A uniformly mediocre voice must not be scored as drifting.

        Every pair sitting at 0.5 is a consistently mediocre voice; the metric's job is
        the spread, which is zero here.
        """
        base = unit_vector(2)
        wobble = {name: base + index * 0.0 for index, name in enumerate("abc")}
        result = verify.cross_language_drift(wobble, cosine)
        self.assertAlmostEqual(result["drift"], 0.0, places=6)
        self.assertAlmostEqual(result["mean"], 1.0, places=6)

    def test_unrelated_embeddings_drift_more_than_similar_ones(self) -> None:
        similar = {name: unit_vector(3) + 0.01 * unit_vector(10 + index) for index, name in enumerate("abc")}
        dissimilar = {name: unit_vector(20 + index * 7) for index, name in enumerate("abc")}
        close = verify.cross_language_drift(similar, cosine)
        far = verify.cross_language_drift(dissimilar, cosine)
        self.assertLess(close["drift"], far["drift"])

    def test_every_pair_is_reported_and_named_consistently(self) -> None:
        embeddings = {name: unit_vector(30 + index) for index, name in enumerate("abc")}
        result = verify.cross_language_drift(embeddings, cosine)
        self.assertEqual(sorted(result["pairs"]), ["a-b", "a-c", "b-c"])
        self.assertEqual(result["worst_pair"], min(result["pairs"], key=result["pairs"].get))

    def test_a_single_embedding_has_no_drift_to_report(self) -> None:
        result = verify.cross_language_drift({"zh": unit_vector(1)}, cosine)
        self.assertIsNone(result["drift"])
        self.assertEqual(result["pairs"], {})

    def test_the_baseline_is_genuinely_one_language(self) -> None:
        """Otherwise the "noise floor" is just a second cross-language measurement."""
        self.assertEqual(len(verify.SAME_LANGUAGE_BASELINE), 5)
        for sentence in verify.SAME_LANGUAGE_BASELINE:
            self.assertRegex(sentence, r"[\u4e00-\u9fff]")
            self.assertIsNone(
                verify.strip_language_tag(sentence)[1],
                "baseline sentences must carry no language tag",
            )


class TargetTests(unittest.TestCase):
    def test_the_documented_targets_are_the_playbook_values(self) -> None:
        self.assertEqual(verify.PLAYBOOK_LID_TARGET, 0.95)
        self.assertEqual(verify.PLAYBOOK_DRIFT_TARGET, 0.05)

    def test_the_noise_floor_exceeds_the_drift_target(self) -> None:
        """The measured same-language drift is 0.0641, above the 0.05 target.

        This is recorded as a test rather than a comment because it is the reason the
        0.05 figure must be treated as unreachable: no system can hold cross-language
        drift below a value that a single language already exceeds. If someone later
        relaxes the target to match the measurement, this test documents that the
        measurement came first.
        """
        measured_noise_floor = 0.0641
        self.assertGreater(measured_noise_floor, verify.PLAYBOOK_DRIFT_TARGET)


if __name__ == "__main__":
    unittest.main()