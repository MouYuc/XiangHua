"""Pure contract tests for the controlled accent-swap verifier."""""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

import accent_swap_verify as verify  # noqa: E402
import tts_regression as regression  # noqa: E402


class AccentSwapContractTests(unittest.TestCase):
    def test_four_levels_are_fixed_and_ordered(self) -> None:
        self.assertEqual(verify.LEVELS, ("off", "light", "medium", "heavy"))

    def test_base_text_contains_protected_compounds(self) -> None:
        for term in ("目的", "但是"):
            self.assertIn(term, verify.BASE_TEXT)

    def test_base_text_contains_words_for_light_and_medium_rules(self) -> None:
        self.assertIn("没有", verify.BASE_TEXT)
        self.assertIn("什么", verify.BASE_TEXT)

    def test_constants_come_from_the_regression_bench(self) -> None:
        # One implementation: the standalone tool must not drift from the fixed bench.
        self.assertIs(verify.BASE_TEXT, regression.SWAP_BASE_TEXT)
        self.assertIs(verify.LEVELS, regression.SWAP_LEVELS)


class SwapSummaryTests(unittest.TestCase):
    @staticmethod
    def _row(level: str, cer: float | None, secs: float | None, error: str = "") -> dict:
        return {
            "level": level,
            "file": "x.wav" if not error else "",
            "error": error,
            "cer": {"status": "ok", "value": cer} if cer is not None else {"status": "skipped", "reason": "no"},
            "secs": {"status": "ok", "value": secs} if secs is not None else {"status": "skipped", "reason": "no"},
        }

    def test_summary_detects_cer_rise_and_secs_stability(self) -> None:
        rows = [
            self._row("off", 0.03, 0.80),
            self._row("light", 0.04, 0.81),
            self._row("medium", 0.14, 0.79),
            self._row("heavy", 0.21, 0.80),
        ]
        summary = regression.summarize_swap(rows)
        self.assertTrue(summary["cer_rises_with_accent"])
        self.assertEqual(summary["cer_off"], 0.03)
        self.assertEqual(summary["cer_heavy"], 0.21)
        self.assertAlmostEqual(summary["secs_spread"], 0.02)
        self.assertEqual(summary["failed_levels"], [])

    def test_summary_reports_failing_levels_without_crashing(self) -> None:
        rows = [
            self._row("off", 0.03, 0.80),
            self._row("light", 0.04, 0.81),
            self._row("medium", None, None, error="RuntimeError: boom"),
            self._row("heavy", 0.21, 0.80),
        ]
        summary = regression.summarize_swap(rows)
        self.assertEqual(summary["failed_levels"], ["medium"])
        self.assertEqual(summary["cer_available"], 3)
        self.assertFalse(summary["cer_rises_with_accent"] is None)  # off/heavy still comparable
        self.assertTrue(summary["cer_rises_with_accent"])

    def test_summary_with_no_data_stays_quiet(self) -> None:
        summary = regression.summarize_swap([])
        self.assertIsNone(summary["cer_off"])
        self.assertIsNone(summary["secs_spread"])
        self.assertFalse(summary["cer_rises_with_accent"])


class RegressionParserTests(unittest.TestCase):
    def test_swap_and_voice_card_flags_exist(self) -> None:
        parser = regression.build_parser()
        args = parser.parse_args(["--no-swap"])
        self.assertTrue(args.no_swap)
        args = parser.parse_args(["--voice-card", "vc_a", "--voice-card", "vc_b"])
        self.assertEqual(args.voice_card, ["vc_a", "vc_b"])
        self.assertFalse(args.no_swap)
        args = parser.parse_args([])
        self.assertFalse(args.no_swap)
        self.assertIsNone(args.voice_card)


if __name__ == "__main__":
    unittest.main()
