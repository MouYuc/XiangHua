"""Unit tests for the fixed TTS regression bench.

These tests intentionally stop at pure logic and fixture structure. They do not import
the synthesis core, load SenseVoice, load ERes2NetV2, or synthesize audio.

Run from the repository root:

    .venv\\Scripts\\python.exe -m unittest backend.tests.test_tts_regression -v
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from collections import Counter
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import tts_regression as regression  # noqa: E402


FIXTURE_PATH = REPO_ROOT / "backend" / "tests" / "fixtures" / "tts_regression_20.json"


class FixtureStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = regression.load_fixture(FIXTURE_PATH)
        cls.cases = cls.fixture["cases"]

    def test_fixture_passes_its_own_contract(self) -> None:
        self.assertEqual(regression.validate_fixture(self.fixture), [])

    def test_fixture_is_exactly_twenty_unique_cases(self) -> None:
        self.assertEqual(len(self.cases), 20)
        self.assertEqual(len({case["id"] for case in self.cases}), 20)

    def test_every_group_has_four_cases(self) -> None:
        counts = Counter(case["group"] for case in self.cases)
        self.assertEqual(
            counts,
            Counter({"clear": 4, "technical": 4, "light": 4, "medium": 4, "heavy": 4}),
        )

    def test_each_case_has_required_metadata(self) -> None:
        for case in self.cases:
            with self.subTest(case=case["id"]):
                for field in ("id", "group", "text", "accent_level"):
                    self.assertIn(field, case)
                    self.assertTrue(case[field])
                self.assertTrue(case.get("expected_prosody") or case.get("notes"))

    def test_seed_is_fixed(self) -> None:
        self.assertEqual(self.fixture["seed"], 20261017)
        self.assertEqual(regression.REGRESSION_SEED, 20261017)

    def test_clear_group_contains_no_yue_lexical_markers(self) -> None:
        clear_cases = [case for case in self.cases if case["group"] == "clear"]
        joined = "\n".join(case["text"] for case in clear_cases)
        self.assertFalse(sorted(set(joined) & regression.YUE_LEXICAL_MARKERS))

    def test_all_protected_collocations_are_present(self) -> None:
        corpus = "\n".join(case["text"] for case in self.cases)
        for term in ("目的", "的确", "于是", "可是", "但是"):
            with self.subTest(term=term):
                self.assertIn(term, corpus)

    def test_training_out_text_is_declared_and_labelled(self) -> None:
        declared = set(self.fixture["coverage"]["out_of_training_ids"])
        self.assertTrue(declared)
        by_id = {case["id"]: case for case in self.cases}
        for case_id in declared:
            self.assertIn(case_id, by_id)
            self.assertIn("训练外", by_id[case_id]["notes"])

    def test_fixture_is_json_serializable(self) -> None:
        encoded = json.dumps(self.fixture, ensure_ascii=False)
        self.assertIn("tts_regression_20", encoded)


class SelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = regression.load_fixture(FIXTURE_PATH)

    def test_default_selection_runs_each_sentence_once(self) -> None:
        cases = regression.select_cases(self.fixture)
        self.assertEqual(len(cases), 20)
        self.assertEqual(len({case["id"] for case in cases}), 20)

    def test_group_filter_keeps_only_the_requested_group(self) -> None:
        cases = regression.select_cases(self.fixture, groups=["technical"], limit=2)
        self.assertEqual(len(cases), 2)
        self.assertTrue(all(case["group"] == "technical" for case in cases))

    def test_group_filter_accepts_multiple_groups_and_preserves_fixture_order(self) -> None:
        cases = regression.select_cases(self.fixture, groups=["heavy", "clear"])
        self.assertEqual({case["group"] for case in cases}, {"heavy", "clear"})
        self.assertEqual([case["id"] for case in cases[:4]], ["clear_01", "clear_02", "clear_03", "clear_04"])

    def test_comma_separated_groups_are_parsed_without_duplicates(self) -> None:
        self.assertEqual(
            regression.parse_groups(["clear,technical", "clear"]),
            ["clear", "technical"],
        )

    def test_unknown_group_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            regression.parse_groups(["singing"])
        with self.assertRaises(ValueError):
            regression.select_cases(self.fixture, groups=["singing"])

    def test_negative_limit_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            regression.select_cases(self.fixture, limit=-1)

    def test_zero_limit_is_valid_and_empty(self) -> None:
        self.assertEqual(regression.select_cases(self.fixture, limit=0), [])


class FixtureValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = regression.load_fixture(FIXTURE_PATH)

    def test_duplicate_id_is_reported(self) -> None:
        broken = copy.deepcopy(self.fixture)
        broken["cases"][1]["id"] = broken["cases"][0]["id"]
        errors = regression.validate_fixture(broken)
        self.assertTrue(any("重复 id" in error for error in errors), errors)

    def test_clear_group_cannot_smuggle_in_yue_text(self) -> None:
        broken = copy.deepcopy(self.fixture)
        broken["cases"][0]["text"] = "我哋今日去图书馆。"
        errors = regression.validate_fixture(broken)
        self.assertTrue(any("clear 组" in error for error in errors), errors)

    def test_protected_terms_must_appear_in_the_fixture(self) -> None:
        broken = copy.deepcopy(self.fixture)
        broken["coverage"]["protected_terms"].append("不存在的搭配")
        errors = regression.validate_fixture(broken)
        self.assertTrue(any("不存在的搭配" in error for error in errors), errors)

    def test_wrong_accent_level_is_reported(self) -> None:
        broken = copy.deepcopy(self.fixture)
        broken["cases"][0]["accent_level"] = "heavy"
        errors = regression.validate_fixture(broken)
        self.assertTrue(any("accent_level" in error for error in errors), errors)


class AudioMetricTests(unittest.TestCase):
    def test_longest_run_counts_consecutive_samples(self) -> None:
        values = [False, True, True, False, True, True, True, False]
        self.assertEqual(regression.longest_true_run(values), 3)

    def test_clipping_ratio_and_longest_run_are_reported(self) -> None:
        audio = np.zeros(1000, dtype=np.float32)
        audio[0:10] = 1.0
        audio[20:25] = -1.0
        metrics = regression.audio_metrics(audio, 1000, expected_duration=1.0)
        self.assertAlmostEqual(metrics["clipping_ratio"], 0.015, places=8)
        self.assertEqual(metrics["longest_clipping_run"], 10)
        self.assertAlmostEqual(metrics["duration"], 1.0, places=6)

    def test_silence_ratio_separates_zeros_from_steady_signal(self) -> None:
        silent = regression.audio_metrics(np.zeros(1000, dtype=np.float32), 1000)
        active = regression.audio_metrics(np.full(1000, 0.5, dtype=np.float32), 1000)
        self.assertEqual(silent["silence_ratio"], 1.0)
        self.assertEqual(active["silence_ratio"], 0.0)

    def test_suspiciously_short_is_derived_only_when_expected_duration_is_known(self) -> None:
        short = regression.audio_metrics(np.zeros(1000, dtype=np.float32), 1000, expected_duration=10.0)
        unknown = regression.audio_metrics(np.zeros(1000, dtype=np.float32), 1000)
        self.assertTrue(short["suspiciously_short"])
        self.assertIsNone(unknown["suspiciously_short"])

    def test_empty_audio_does_not_divide_by_zero(self) -> None:
        metrics = regression.audio_metrics(np.zeros(0, dtype=np.float32), 1000)
        self.assertEqual(metrics["duration"], 0.0)
        self.assertIsNone(metrics["clipping_ratio"])
        self.assertIsNone(metrics["silence_ratio"])


class GroupSummaryTests(unittest.TestCase):
    def make_case(self, case_id: str, group: str, status: str, **overrides):
        metrics = {
            "duration": 2.0,
            "expected_duration": 2.2,
            "suspiciously_short": False,
            "clipping_ratio": 0.0,
            "longest_clipping_run": 0,
            "silence_ratio": 0.1,
        }
        cer = {"status": "ok", "value": 0.1}
        secs = {"status": "ok", "value": 0.8}
        for key, value in overrides.items():
            if key in metrics:
                metrics[key] = value
            elif key == "cer":
                cer = value
            elif key == "secs":
                secs = value
        return {
            "id": case_id,
            "group": group,
            "status": status,
            "metrics": metrics,
            "cer": cer,
            "secs": secs,
        }

    def test_group_summary_means_counts_and_failures(self) -> None:
        cases = [
            self.make_case("c1", "clear", "ok", cer={"status": "ok", "value": 0.1}),
            self.make_case("c2", "clear", "failed", cer={"status": "skipped", "value": None}),
        ]
        summary = regression.summarize_case_set(cases)
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["ok"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["cer"]["available"], 1)
        self.assertAlmostEqual(summary["cer"]["mean"], 0.1)
        self.assertAlmostEqual(summary["duration"]["mean"], 2.0)

    def test_group_summary_keeps_groups_apart(self) -> None:
        cases = [
            self.make_case("c1", "clear", "ok"),
            self.make_case("h1", "heavy", "ok", cer={"status": "skipped", "value": None}),
        ]
        groups = regression.summarize_groups(cases)
        self.assertEqual(list(groups), ["clear", "heavy"])
        self.assertEqual(groups["clear"]["cer"]["available"], 1)
        self.assertEqual(groups["heavy"]["cer"]["available"], 0)

    def test_suspicious_and_clipping_cases_are_counted(self) -> None:
        cases = [
            self.make_case("c1", "clear", "ok", suspiciously_short=True, longest_clipping_run=4),
            self.make_case("c2", "clear", "ok"),
        ]
        summary = regression.summarize_case_set(cases)
        self.assertEqual(summary["suspiciously_short"], 1)
        self.assertEqual(summary["clipping_cases"], 1)


class AsrCleanupTests(unittest.TestCase):
    def test_sensevoice_control_tokens_are_removed_before_cer(self) -> None:
        raw = "<|zh|><|NEUTRAL|><|Speech|><|withitn|>今天天气很好。"
        self.assertEqual(regression.clean_asr_hypothesis(raw), "今天天气很好。")

    def test_clean_asr_keeps_ordinary_angle_text(self) -> None:
        self.assertEqual(regression.clean_asr_hypothesis("a < b"), "a < b")


if __name__ == "__main__":
    unittest.main()
