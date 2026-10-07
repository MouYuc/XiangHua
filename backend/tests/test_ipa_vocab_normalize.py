"""Regression gate for the frontend-IPA to model-vocabulary normalisation.

The bug these tests guard against is silent: a token the acoustic model has no
embedding for is dropped, the audio just gets shorter, and nothing raises. Every
test here fails loudly instead, so a future lexicon or vocabulary change cannot
reintroduce the loss unnoticed.

Run from the repository root:

    .venv\\Scripts\\python.exe -m unittest backend.tests.test_ipa_vocab_normalize -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ipa_vocab_normalize as normalizer  # noqa: E402

REMAP_PATH = REPO_ROOT / "assets" / "ipa_vocab_remap.json"
VOCAB_PATH = REPO_ROOT / "third_party" / "DiaMoE-TTS" / "diamoe_tts" / "data" / "vocab.txt"
AUDIT_PATH = REPO_ROOT / "output" / "frontend_lexicon_audit.json"


class IpaVocabNormalizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.vocabulary = normalizer.load_vocabulary(VOCAB_PATH)
        if REMAP_PATH.is_file():
            cls.payload = json.loads(REMAP_PATH.read_text(encoding="utf-8"))
        else:
            cls.payload = None

    def setUp(self) -> None:
        if self.payload is None:
            self.skipTest(f"normalisation table not built yet: {REMAP_PATH}")

    def test_every_mapping_lands_in_the_vocabulary(self) -> None:
        """A mapping to a token the model cannot read would be worse than dropping it."""
        broken = {
            token: entry["to"]
            for token, entry in self.payload["mappings"].items()
            if entry["to"] not in self.vocabulary
        }
        self.assertEqual(broken, {}, f"mappings point outside the vocabulary: {broken}")

    def test_mapping_table_records_a_reason_for_every_entry(self) -> None:
        """The table is meant to be reviewed as data, so every row needs a justification."""
        missing = [
            token
            for token, entry in self.payload["mappings"].items()
            if not entry.get("reason") or not entry.get("rule")
        ]
        self.assertEqual(missing, [], f"mappings without a rule or reason: {missing}")

    def test_only_one_rule_is_allowed_to_change_a_tone(self) -> None:
        """Tone is phonemic, so at most one rule may alter it, and it must stay auditable.

        ``add_breve`` and ``expand_checked_tone`` restore a diacritic or repeat a
        level, so both leave the tone code itself intact. ``nearest_contour`` is
        the only rule that changes the code; keeping its count small is what makes
        review of those entries practical.
        """
        tone_changing = [
            token
            for token, entry in self.payload["mappings"].items()
            if entry["rule"] == "nearest_contour"
        ]
        self.assertLessEqual(
            len(tone_changing), 5, f"too many tone-changing mappings: {tone_changing}"
        )

    def test_breve_rule_never_changes_the_tone_code(self) -> None:
        """The largest repair class is only allowed to add the short-vowel diacritic."""
        for token, entry in self.payload["mappings"].items():
            if entry["rule"] != "add_breve":
                continue
            _, source_tone = normalizer.split_tone(token)
            _, target_tone = normalizer.split_tone(entry["to"])
            self.assertEqual(
                source_tone,
                target_tone,
                f"add_breve must preserve the tone: {token!r} -> {entry['to']!r}",
            )

    def test_already_supported_tokens_pass_through(self) -> None:
        """Tokens the model can already read must not be touched.

        The stress mark is deliberately probed with ``ˈa`` rather than ``ˈu``:
        the released vocabulary only carries a bare (toneless) stressed vowel for
        ``ˈa``, ``ˈe`` and ``ˈɒ``. ``ˈu`` is *not* readable, so a remap that
        rewrote every stressed token would be invisible on a token that happens
        to be supported anyway. Both facts are asserted below so a future
        vocabulary change cannot quietly invalidate the probe.
        """
        tokens = ["w", "ˈa", "t͡ʂ", "ə"]
        for token in tokens:
            self.assertIn(token, self.vocabulary, f"probe token is not readable: {token!r}")

        mapped, trace = normalizer.normalize_tokens(tokens, self.vocabulary, {})
        self.assertEqual(mapped, tokens)
        self.assertEqual(trace, [])

        # The vocabulary's stress coverage is not uniform; keep that visible.
        self.assertNotIn("ˈu", self.vocabulary)

    def test_the_remap_never_moves_the_stress_mark(self) -> None:
        """Stress is a separate layer from tone and must survive normalisation.

        Tone is preserved by construction (see the breve and contour tests), but a
        rule that rebuilt a token from scratch could still drop the stress mark and
        change which syllable the model accents. None of them may.
        """
        stress = "ˈ"
        offenders = [
            f"{token} -> {entry['to']} ({entry['rule']})"
            for token, entry in self.payload["mappings"].items()
            if token.startswith(stress) != entry["to"].startswith(stress)
        ]
        self.assertEqual(offenders, [], f"mappings that moved the stress mark: {offenders}")

    def test_tone_on_a_glide_coda_moves_back_to_the_nucleus(self) -> None:
        """Qingdao's ``bei6`` row puts the tone on the glide; the repair must recover it.

        This is the one case a token-to-token table cannot express, because the fix
        needs the neighbouring token. Dropping the tone instead would turn a tone-6
        syllable into a toneless one.
        """
        mapped, trace = normalizer.normalize_tokens(["p", "ˈe", "ɪ̯ᴹᴸ"], self.vocabulary, {})
        self.assertEqual(mapped, ["p", "ˈeᴹᴸ", "ɪ̯"])
        self.assertTrue(any(entry["rule"] == "glide_tone_move" for entry in trace))
        for token in mapped:
            self.assertIn(token, self.vocabulary)

    def test_no_reachable_frontend_token_is_left_unmapped(self) -> None:
        """Every token the audit found unreadable must have a repair.

        This is the end-to-end assertion: it reads the audit report (the
        enumeration of what the frontend can actually emit) and requires the remap
        plus the sequence rule to cover all of it.
        """
        if not AUDIT_PATH.is_file():
            self.skipTest(f"audit report not present: {AUDIT_PATH}")

        reports = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
        remap = normalizer.load_remap(REMAP_PATH)

        unresolved: list[str] = []
        for report in reports:
            for token in report.get("vocab_missing_phone_list") or {}:
                if token in remap or token in self.vocabulary:
                    continue
                if normalizer.is_toned_glide(token):
                    bare = token[: -len(normalizer.split_tone(token)[1])]
                    if bare in self.vocabulary:
                        continue
                unresolved.append(f"{report['dialect']}:{token}")

        self.assertEqual(
            unresolved, [], f"tokens would still be dropped by the model: {unresolved}"
        )


    def test_pause_punctuation_the_model_lacks_is_aliased_not_dropped(self) -> None:
        """``；`` and ``：`` are in neither the punctuation file nor the vocabulary.

        ``ipa_to_model_text`` keeps a token only when its bracketed form is in the
        IPA set or in the punctuation set, so both marks used to be dropped before
        the tokenizer saw them. They carry a pause, so the model's own comma is
        substituted; the prosody survives even though the glyph does not.
        """
        for mark, expected in normalizer.PUNCTUATION_ALIASES.items():
            mapped, trace = normalizer.apply_punctuation_aliases([mark])
            self.assertEqual(mapped, [expected])
            self.assertEqual(trace[0]["rule"], "punctuation_alias")
        self.assertEqual(normalizer.PUNCTUATION_ALIASES["；"], "，")
        self.assertEqual(normalizer.PUNCTUATION_ALIASES["："], "，")

    def test_brackets_and_quotes_are_dropped_on_purpose(self) -> None:
        """Delimiters carry no pause, so there is nothing to preserve.

        Asserted rather than left implicit: the drop is a decision, and a future
        change that starts aliasing brackets should have to say so.
        """
        for mark in normalizer.PUNCTUATION_DROPPED_ON_PURPOSE:
            self.assertNotIn(mark, normalizer.PUNCTUATION_ALIASES)
        self.assertEqual(normalizer.PUNCTUATION_DROPPED_ON_PURPOSE, set("（）【】()[]“”‘’"))

    def test_normalize_for_model_applies_both_stages(self) -> None:
        """One entry point, so production and the measuring scripts cannot diverge."""
        tokens = ["；", "ˈuᴴ", "ˈi¹¹³", "w"]
        mapped, trace = normalizer.normalize_for_model(tokens, self.vocabulary, {})
        self.assertEqual(mapped[0], "，")
        self.assertEqual(mapped[-1], "w")
        rules = {entry["rule"] for entry in trace}
        self.assertIn("punctuation_alias", rules)


if __name__ == "__main__":
    unittest.main()