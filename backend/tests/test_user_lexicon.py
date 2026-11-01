"""User pronunciation lexicon: storage service and pronunciation-layer override."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services import user_lexicon
from backend.services.pronunciation import build_pronunciation_plan


class LexiconServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "lexicon.json"
        self.addCleanup(self._tmp.cleanup)

    def test_missing_file_reads_as_empty(self) -> None:
        self.assertEqual(user_lexicon.active_overrides(self.path), {})
        self.assertEqual(user_lexicon.list_entries(self.path), [])

    def test_upsert_list_delete_roundtrip(self) -> None:
        user_lexicon.upsert_entry("重创", "虫创", note="chóng chuàng", path=self.path)
        user_lexicon.upsert_entry("圣辉之剑", "剩辉之见", path=self.path)
        rows = user_lexicon.list_entries(self.path)
        self.assertEqual({row["term"] for row in rows}, {"重创", "圣辉之剑"})
        self.assertEqual(
            user_lexicon.active_overrides(self.path),
            {"重创": "虫创", "圣辉之剑": "剩辉之见"},
        )
        self.assertTrue(user_lexicon.delete_entry("重创", path=self.path))
        self.assertFalse(user_lexicon.delete_entry("不存在", path=self.path))
        self.assertEqual(user_lexicon.active_overrides(self.path), {"圣辉之剑": "剩辉之见"})

    def test_disabled_entry_is_not_active(self) -> None:
        user_lexicon.upsert_entry("重创", "虫创", enabled=False, path=self.path)
        self.assertEqual(user_lexicon.active_overrides(self.path), {})
        rows = user_lexicon.list_entries(self.path)
        self.assertEqual(rows[0]["term"], "重创")
        self.assertFalse(rows[0]["enabled"])

    def test_validation_rejects_bad_entries(self) -> None:
        for term, replacement in (("", "x"), ("x", ""), ("同", "同"), ("长" * 33, "x"), ("x", "长" * 129)):
            with self.assertRaises(user_lexicon.LexiconError, msg=(term[:5], replacement[:5])):
                user_lexicon.validate_entry(term, replacement)

    def test_corrupt_file_degrades_to_empty(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(user_lexicon.active_overrides(self.path), {})
        user_lexicon.upsert_entry("甲", "A", path=self.path)
        self.assertEqual(user_lexicon.active_overrides(self.path), {"甲": "A"})


class UserOverridesInPlanTests(unittest.TestCase):
    def test_overrides_apply_even_at_off_level(self) -> None:
        plan = build_pronunciation_plan(
            "这把武器造成了重创。", language="zh", accent_level="off",
            user_overrides={"重创": "虫创"},
        )
        self.assertEqual(plan.rendered_text, "这把武器造成了虫创。")
        self.assertEqual(plan.source_text, "这把武器造成了重创。")
        self.assertEqual(plan.replacements[0]["origin"], "user_lexicon")

    def test_overrides_beat_accent_rules_and_protection(self) -> None:
        plan = build_pronunciation_plan(
            "目的很清楚，是不是重创了敌人。",
            language="zh",
            accent_level="medium",
            user_overrides={"目的": "木滴", "重创": "虫创"},
        )
        rendered = plan.rendered_text
        self.assertIn("木滴", rendered)
        self.assertIn("虫创", rendered)
        # accent rules still apply to the rest of the line
        self.assertIn("唔系", rendered)
        origins = {item["source"]: item["origin"] for item in plan.replacements}
        self.assertEqual(origins["目的"], "user_lexicon")
        self.assertEqual(origins["是不是"], "accent")

    def test_no_overrides_keeps_legacy_off_behaviour(self) -> None:
        plan = build_pronunciation_plan("今天天气真不错。", language="zh", accent_level="off")
        self.assertEqual(plan.rendered_text, "今天天气真不错。")
        self.assertEqual(plan.replacements, [])

    def test_span_offsets_still_point_at_source(self) -> None:
        plan = build_pronunciation_plan(
            "重创目标。", language="zh", accent_level="off", user_overrides={"重创": "虫创"}
        )
        for item in plan.replacements:
            self.assertEqual("重创目标。"[item["start"] : item["end"]], item["source"])


if __name__ == "__main__":
    unittest.main()
