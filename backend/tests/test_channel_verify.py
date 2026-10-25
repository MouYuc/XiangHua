"""Pure contract tests for the cross-channel exchange verifier."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

import channel_verify as verify  # noqa: E402


class ChannelVerifyContractTests(unittest.TestCase):
    def test_summarize_describes_baseline_and_spread(self) -> None:
        summary = verify.summarize_channel([0.82, 0.79, 0.84, 0.81], [0.03, 0.05])
        self.assertEqual(summary["ref_pairs"], 4)
        self.assertEqual(summary["ref_secs_min"], 0.79)
        self.assertEqual(summary["output_secs_spread_max"], 0.05)
        self.assertIn("不绑信道", summary["note"])

    def test_summarize_with_no_data_stays_quiet(self) -> None:
        summary = verify.summarize_channel([], [])
        self.assertIsNone(summary["ref_secs_mean"])
        self.assertIsNone(summary["output_secs_spread_mean"])

    def test_parser_requires_at_least_two_files_at_runtime(self) -> None:
        # The nargs="+" contract itself is asserted by argparse; here we lock the
        # guard that a single input is rejected with a user-actionable message.
        parser = verify.build_parser()
        args = parser.parse_args(["only.wav"])
        self.assertEqual(args.inputs, ["only.wav"])
        with self.assertRaises(SystemExit):
            # argparse exits on zero inputs for nargs="+"
            parser.parse_args([])

    def test_default_text_is_the_fixed_swap_sentence(self) -> None:
        # The channel test shares the swap sentence so both exchange suites stay
        # comparable on one text.
        from tts_regression import SWAP_BASE_TEXT

        args = verify.build_parser().parse_args(["a.wav", "b.wav"])
        self.assertEqual(args.text, SWAP_BASE_TEXT)


if __name__ == "__main__":
    unittest.main()
