"""Contract tests for the staged pronunciation layer.

The pronunciation layer is the first hard boundary of the four-layer architecture:
it may rewrite text for an accent, but it must never rewrite a protected compound.
These tests pin the four levels, original-text offsets, longest-match behaviour and
the legacy ``to_lightmid`` entry point.

Run from the repository root:

    .venv\\Scripts\\python.exe -m unittest backend.tests.test_pronunciation -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services.pronunciation import (  # noqa: E402
    AccentLevel,
    PronunciationPlan,
    build_pronunciation_plan,
)
from tools.rules import safe_name, to_lightmid  # noqa: E402


class AccentLevelTests(unittest.TestCase):
    def test_the_public_values_are_exactly_the_four_levels(self) -> None:
        self.assertEqual(
            [level.value for level in AccentLevel],
            ["off", "light", "medium", "heavy"],
        )

    def test_invalid_levels_raise_value_error(self) -> None:
        for invalid in ("invalid", "HEAVY", "", None):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    build_pronunciation_plan("你好", accent_level=invalid)

    def test_enum_members_are_accepted(self) -> None:
        plan = build_pronunciation_plan("你好", accent_level=AccentLevel.LIGHT)
        self.assertEqual(plan.accent_intensity, 0.35)


class PronunciationPlanTests(unittest.TestCase):
    def test_off_returns_the_original_text_and_no_replacements(self) -> None:
        source = "这是我的目的，但是我还是不是你的。"
        plan = build_pronunciation_plan(source, accent_level="off")
        self.assertEqual(plan.source_text, source)
        self.assertEqual(plan.rendered_text, source)
        self.assertEqual(plan.replacements, [])
        self.assertEqual(plan.accent_intensity, 0.0)

    def test_light_only_uses_unambiguous_phrases(self) -> None:
        source = "你是不是什么人？我没有说最重要的事。"
        plan = build_pronunciation_plan(source, accent_level="light")
        self.assertEqual(plan.rendered_text, "你系唔系咩人？我冇说最紧要的事。")
        self.assertEqual(
            [item["source"] for item in plan.replacements],
            ["是不是", "什么人", "没有", "最重要"],
        )

    def test_medium_adds_medium_rules_without_global_de_or_shi(self) -> None:
        source = "这是什么？没你说得那么重要。"
        plan = build_pronunciation_plan(source, accent_level="medium")
        self.assertEqual(plan.rendered_text, "这是乜嘢？冇你话得那么重要。")
        self.assertIn("这是", plan.rendered_text)
        self.assertNotIn("嘅", plan.rendered_text)

    def test_heavy_adds_global_rules_but_keeps_protected_terms(self) -> None:
        source = "这是我的目的，但是我还是不是你的。"
        plan = build_pronunciation_plan(source, accent_level="heavy")
        self.assertEqual(plan.rendered_text, "这系我嘅目的，但是我还是唔系你嘅。")

    def test_every_required_fixed_compound_is_left_untouched(self) -> None:
        protected = "目的的确于是可是但是有的要是总是只是还是"
        plan = build_pronunciation_plan(protected, accent_level="heavy")
        self.assertEqual(plan.rendered_text, protected)
        self.assertEqual(plan.replacements, [])
        for term in (
            "目的",
            "的确",
            "于是",
            "可是",
            "但是",
            "有的",
            "是的",
            "要是",
            "总是",
            "只是",
            "还是",
        ):
            with self.subTest(term=term):
                term_plan = build_pronunciation_plan(term, accent_level="heavy")
                self.assertEqual(term_plan.rendered_text, term)
                self.assertEqual(term_plan.replacements, [])
                self.assertIn(term, term_plan.protected_terms)

    def test_longest_phrase_wins_before_single_character_rules(self) -> None:
        plan = build_pronunciation_plan("是不是什么人最重要", accent_level="light")
        self.assertEqual(plan.rendered_text, "系唔系咩人最紧要")
        self.assertNotIn("是", [item["source"] for item in plan.replacements])

    def test_replacement_offsets_point_into_the_original_text(self) -> None:
        source = "你是不是什么人？我没有说最重要的事。"
        plan = build_pronunciation_plan(source, accent_level="light")
        previous_end = 0
        for item in plan.replacements:
            start = item["start"]
            end = item["end"]
            self.assertIsInstance(start, int)
            self.assertIsInstance(end, int)
            self.assertGreaterEqual(start, previous_end)
            self.assertLessEqual(end, len(source))
            self.assertEqual(source[start:end], item["source"])
            previous_end = end

    def test_non_chinese_text_passes_through_unchanged(self) -> None:
        source = "Hello, 是不是？"
        plan = build_pronunciation_plan(
            source, language="en", accent_level="heavy"
        )
        self.assertEqual(plan.rendered_text, source)
        self.assertEqual(plan.language, "en")
        self.assertEqual(plan.replacements, [])
        self.assertEqual(plan.protected_terms, [])
        self.assertEqual(plan.accent_intensity, 0.0)
        self.assertEqual(plan.accent_pack_id, "none")

    def test_to_dict_is_json_serialisable_and_returns_a_copy(self) -> None:
        plan = build_pronunciation_plan("是不是", accent_level="light")
        payload = plan.to_dict()
        json.dumps(payload, ensure_ascii=False)
        self.assertIsInstance(plan, PronunciationPlan)
        self.assertEqual(payload["source_text"], "是不是")
        self.assertEqual(payload["rendered_text"], "系唔系")
        payload["replacements"].clear()
        self.assertEqual(len(plan.to_dict()["replacements"]), 1)


class LegacyRuleCompatibilityTests(unittest.TestCase):
    def test_to_lightmid_delegates_to_the_heavy_level(self) -> None:
        source = "这是我的目的，是不是？"
        expected = build_pronunciation_plan(
            source, accent_level="heavy"
        ).rendered_text
        self.assertEqual(expected, "这系我嘅目的，系唔系？")
        self.assertEqual(to_lightmid(source), expected)

    def test_safe_name_behaviour_is_unchanged(self) -> None:
        self.assertEqual(safe_name("  bad/name:*? "), "badname")
        self.assertEqual(safe_name("abcdefghijklmnop"), "abcdefghijkl")
        self.assertEqual(safe_name("///"), "line")


if __name__ == "__main__":
    unittest.main()