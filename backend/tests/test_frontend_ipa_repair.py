"""Regression gate for the Defect A repair (Mandarin fallback for lost syllables).

The bug: a character absent from a dialect's ``hanzi.xlsx`` falls back to its
Mandarin pinyin, ``liandu_tone`` rewrites the tone, ``pinyin2ipa`` misses the
dialect table and emits the raw pinyin, and the acoustic model - having no
embedding for ``zhe7`` - drops it. Nothing raises; the character is simply not
spoken. On the Shanghai table that costs 131 syllables in a 158-sentence corpus,
``这`` more than any other character.

The repair replaces such a syllable with the **Mandarin** IPA from the vendor's
own ``putonghua/base_syllable.xlsx``. It deliberately does not synthesise dialect
IPA: Shanghai's ``syllable.xlsx`` has no ``zhe``, ``mei``, ``zhan``, ``wei`` or
``shen`` family at all, so anything dialect-shaped would be fabrication.

These tests pin the properties that make the repair safe rather than the exact
table contents, which are vendor data and may be replaced.

Run from the repository root:

    .venv\\Scripts\\python.exe -m unittest backend.tests.test_frontend_ipa_repair -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import frontend_ipa_repair as repair  # noqa: E402

FRONTEND = REPO_ROOT / "third_party" / "DiaMoE-TTS" / "dialect_frontend"
SHANGHAI = FRONTEND / "frontend" / "dialect" / "shanghai"


class MandarinTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mandarin = repair.Mandarins(FRONTEND)

    def test_the_vendor_ships_a_usable_mandarin_table(self) -> None:
        """The whole repair rests on this table existing and being populated."""
        self.assertGreater(len(self.mandarin.syllables), 1000)
        self.assertGreater(len(self.mandarin.tone_map), 0)

    def test_a_mandarin_syllable_becomes_ipa(self) -> None:
        chunk = self.mandarin.ipa("zhe4")
        self.assertIsNotNone(chunk)
        assert chunk is not None
        self.assertNotIn(",", chunk, "commas must already be rewritten to spaces")
        self.assertNotIn("|", chunk, "a substitute is one chunk, never two")
        for token in chunk.split():
            self.assertFalse(
                repair._PINYIN_LIKE.match(token),
                f"substituted chunk still contains raw pinyin: {chunk!r}",
            )

    def test_a_tone_the_mandarin_table_lacks_returns_nothing(self) -> None:
        """Falling back must never invent a syllable out of a base plus a guess."""
        self.assertIsNone(self.mandarin.ipa("zhe11"))

    def test_an_unknown_syllable_returns_nothing(self) -> None:
        self.assertIsNone(self.mandarin.ipa("qqq1"))


class FallbackDetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.shanghai = repair.load_syllable_table(SHANGHAI / "syllable.xlsx")
        cls.mandarin = repair.Mandarins(FRONTEND)

    @staticmethod
    def _write(path: Path, rows: list[tuple[str, str, str]]) -> Path:
        path.write_text(
            "\n".join("\t".join(row) for row in rows) + "\n", encoding="utf-8"
        )
        return path

    def test_a_syllable_absent_from_the_dialect_table_is_flagged(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            known = self.known_syllable()
            stage2 = self._write(
                Path(raw) / "shanghai_hanzi.txt", [("p1", "走这", f"{known} zhe4")]
            )
            fallbacks, stats = repair.find_fallbacks(stage2, self.shanghai)
            self.assertEqual(stats["lines"], 1)
            # The dialect syllable resolves; the Mandarin fallback does not.
            self.assertIn("p1", fallbacks)
            self.assertEqual(list(fallbacks["p1"]), [1])
            self.assertEqual(fallbacks["p1"][1], "zhe4")

    def test_the_mandarin_tone_is_read_before_sandhi_can_rewrite_it(self) -> None:
        """Reading stage 2 is the point: ``liandu_tone`` destroys this tone."""
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            stage2 = self._write(Path(raw) / "shanghai_hanzi.txt", [("p1", "尾", "wei3")])
            fallbacks, _ = repair.find_fallbacks(stage2, self.shanghai)
            self.assertEqual(fallbacks["p1"][0], "wei3")
            self.assertIsNotNone(self.mandarin.ipa("wei3"))

    def test_a_line_whose_token_count_differs_is_not_guessed_at(self) -> None:
        """A word-level replacement can change the count, so index is not a character."""
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            stage2 = self._write(Path(raw) / "shanghai_hanzi.txt", [("p1", "走这", "zhe4")])
            fallbacks, stats = repair.find_fallbacks(stage2, self.shanghai)
            self.assertEqual(fallbacks, {})
            self.assertEqual(stats["skipped_unaligned"], 1)

    def test_punctuation_is_never_treated_as_a_syllable(self) -> None:
        """``，`` is not a pinyin string, so it must not be sent to the Mandarin table."""
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            known = self.known_syllable()
            stage2 = self._write(
                Path(raw) / "shanghai_hanzi.txt", [("p1", "走，", f"{known} ，")]
            )
            fallbacks, _ = repair.find_fallbacks(stage2, self.shanghai)
            self.assertEqual(fallbacks, {})

    def known_syllable(self) -> str:
        """A pinyin entry the Shanghai table really has, taken from the table itself.

        Hard-coding one would rot the moment the vendor tables are replaced, and
        ``zou3`` - the obvious choice for 走 - is in fact *absent*, which is the
        defect being measured.
        """
        return next(iter(self.shanghai))


class SubstitutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mandarin = repair.Mandarins(FRONTEND)

    def test_a_raw_pinyin_chunk_is_replaced(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            final = tmp / "shanghai_ipa_format.txt"
            # One chunk per character: 走 resolved, 这 did not.
            final.write_text("p1\t走这\tz ˈɤᴹᴹ | zhe4\n", encoding="utf-8")
            rows, report = repair.repair(final, {"p1": {1: "zhe4"}}, self.mandarin)
            self.assertEqual(report["substituted"], 1)
            self.assertNotIn("zhe4", rows["p1"][1])
            self.assertEqual(len(rows["p1"][1].split("|")), 2, "chunk count must not change")
            self.assertEqual(rows["p1"][1].split("|")[0].strip(), "z ˈɤᴹᴹ")
            self.assertEqual(report["by_character"], {"这": 1})

    def test_a_chunk_that_is_no_longer_pinyin_is_left_alone(self) -> None:
        """Verification before substitution: the position may have been rewritten."""
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            final = tmp / "shanghai_ipa_format.txt"
            final.write_text("p1\t走这\tz ˈɤᴹᴹ | t͜s ˈɤᴸᴸ\n", encoding="utf-8")
            rows, report = repair.repair(final, {"p1": {1: "zhe4"}}, self.mandarin)
            self.assertEqual(report["substituted"], 0)
            self.assertEqual(report["skipped_chunk_not_pinyin"], 1)
            self.assertEqual(rows["p1"][1], "z ˈɤᴹᴹ | t͜s ˈɤᴸᴸ")

    def test_a_line_whose_chunk_count_changed_is_skipped_whole(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            final = tmp / "shanghai_ipa_format.txt"
            final.write_text("p1\t走这\tz ˈɤᴹᴹ\n", encoding="utf-8")
            rows, report = repair.repair(final, {"p1": {1: "zhe4"}}, self.mandarin)
            self.assertEqual(report["skipped_line_length_changed"], 1)
            self.assertEqual(rows["p1"][1], "z ˈɤᴹᴹ")

    def test_an_unmappable_pinyin_is_reported_not_dropped(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            final = tmp / "shanghai_ipa_format.txt"
            final.write_text("p1\t走这\tz ˈɤᴹᴹ | zhe11\n", encoding="utf-8")
            rows, report = repair.repair(final, {"p1": {1: "zhe11"}}, self.mandarin)
            self.assertEqual(report["substituted"], 0)
            self.assertIn("zhe11", report["unmapped_by_mandarin_table"])
            self.assertEqual(rows["p1"][1], "z ˈɤᴹᴹ | zhe11", "left visible, not blanked")

    def test_the_repaired_chunk_is_readable_by_the_model(self) -> None:
        """The point of the exercise: the substitute must have an embedding."""
        import tempfile

        from diamoe_runtime import load_symbol_tables

        ipa_set, punctuation = load_symbol_tables(REPO_ROOT / "third_party" / "DiaMoE-TTS")
        bare_punctuation = {entry[1:-1] for entry in punctuation}
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            final = tmp / "shanghai_ipa_format.txt"
            final.write_text("p1\t走这\tz ˈɤᴹᴹ | zhe4\n", encoding="utf-8")
            rows, _ = repair.repair(final, {"p1": {1: "zhe4"}}, self.mandarin)
            chunk = rows["p1"][1].split("|")[1].strip()
            for token in chunk.split():
                symbol = f"[{token}]"
                self.assertTrue(
                    symbol in ipa_set or token in bare_punctuation,
                    f"{token!r} would still be dropped by the model",
                )


class ControlDialectTests(unittest.TestCase):
    def test_putonghua_is_left_untouched(self) -> None:
        """Mandarin has no dialect table to miss, so there is nothing to fall back from."""
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            report = repair.repair_dialect("putonghua", Path(raw), FRONTEND)
            self.assertEqual(report.get("skipped"), "control dialect")


if __name__ == "__main__":
    unittest.main()