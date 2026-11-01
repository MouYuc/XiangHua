"""Integration tests for staged pronunciation in the synthesis config path."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend import app as app_module
from backend.api import deps
from backend.schemas import ConfigUpdate
from backend.services import core
from backend.services import reference_prompt as reference_prompt_module
from backend.services.pronunciation import PROTECTED_TERMS, PronunciationPlan


def synthesis_config(level: str = "heavy", language: str = "zh") -> dict[str, object]:
    return {
        "text_lang": language,
        "prompt_lang": "zh",
        "accent_level": level,
        "auto_lightmid": False,
        "temperature": 0.95,
        "sample_steps": 64,
        "speed": 1.0,
    }


class AccentConfigTests(unittest.TestCase):
    def test_default_config_is_clear_first(self) -> None:
        config = core.default_config()
        self.assertEqual(config["accent_level"], "off")
        self.assertFalse(config["auto_lightmid"])

    def test_schema_accepts_only_the_four_levels(self) -> None:
        for level in ("off", "light", "medium", "heavy"):
            self.assertEqual(ConfigUpdate(accent_level=level).accent_level, level)
        with self.assertRaises(ValidationError):
            ConfigUpdate(accent_level="lightmid")


class RenderAccentTests(unittest.TestCase):
    def test_protected_compounds_survive_heavy(self) -> None:
        text = "这个方案的目的的确是为了提高效率，但是我们还要验证。"
        rendered = core.render_synthesis_text(
            text,
            {"text_lang": "zh", "accent_level": "heavy"},
        )
        for term in ("目的", "的确", "但是"):
            self.assertIn(term, rendered)
        self.assertIn("系为了", rendered)

    def test_heavy_no_longer_rewrites_the_protected_but(self) -> None:
        rendered = core.render_synthesis_text(
            "但是我还是想去。",
            {"text_lang": "zh", "accent_level": "heavy"},
        )
        self.assertTrue(rendered.startswith("但是"), rendered)

    def test_light_and_medium_do_not_touch_global_de_or_shi(self) -> None:
        text = "这是我的东西，是不是？"
        light = core.render_synthesis_text(text, {"text_lang": "zh", "accent_level": "light"})
        medium = core.render_synthesis_text(text, {"text_lang": "zh", "accent_level": "medium"})
        self.assertIn("的", light)
        self.assertIn("的", medium)
        self.assertIn("系唔系", medium)

    def test_legacy_auto_lightmid_maps_to_medium_not_heavy(self) -> None:
        rendered = core.render_synthesis_text(
            "这是我的东西，是不是？",
            {"text_lang": "zh", "auto_lightmid": True},
        )
        self.assertIn("的", rendered)
        self.assertIn("系唔系", rendered)

    def test_non_chinese_is_untouched(self) -> None:
        text = "This is a test."
        self.assertEqual(
            core.render_synthesis_text(text, {"text_lang": "en", "accent_level": "heavy"}),
            text,
        )

    def test_mixed_line_applies_accent_only_to_chinese(self) -> None:
        segments = core.parse_language_segments("<zh>是不是这样</zh><en>No, it is not.</en>")
        rendered = core.render_language_segments(
            segments,
            {"text_lang": "zh", "accent_level": "medium"},
        )
        self.assertEqual(rendered[0][1], "系唔系这样")
        self.assertEqual(rendered[1][1], "No, it is not.")


class RenderWithPlanTests(unittest.TestCase):
    def test_plan_and_plain_renderer_return_the_same_text(self) -> None:
        cfg = {"text_lang": "zh", "accent_level": "medium"}
        plan = core.render_with_plan("这是什么？", cfg)
        self.assertIsInstance(plan, PronunciationPlan)
        self.assertEqual(plan.rendered_text, core.render_synthesis_text("这是什么？", cfg))
        self.assertEqual(plan.rendered_text, "这是乜嘢？")
        self.assertEqual(plan.source_text, "这是什么？")
        self.assertEqual(plan.lexicon_id, "lex_zh_base_v1")
        self.assertEqual(plan.accent_pack_id, "guangpu_medium")
        self.assertAlmostEqual(plan.accent_intensity, 0.65)
        self.assertEqual(plan.protected_terms, list(PROTECTED_TERMS))

    def test_legacy_auto_lightmid_builds_a_medium_plan(self) -> None:
        plan = core.render_with_plan("是不是", {"text_lang": "zh", "auto_lightmid": True})
        self.assertEqual(plan.rendered_text, "系唔系")
        self.assertEqual(plan.accent_pack_id, "guangpu_medium")
        self.assertAlmostEqual(plan.accent_intensity, 0.65)

    def test_non_chinese_plan_has_no_lexicon_and_keeps_text(self) -> None:
        for language, text in (("en", "This is a test."), ("ja", "これはテストです。")):
            with self.subTest(language=language):
                plan = core.render_with_plan(text, {"text_lang": language, "accent_level": "heavy"})
                self.assertEqual(plan.lexicon_id, "none")
                self.assertEqual(plan.accent_pack_id, "none")
                self.assertEqual(plan.accent_intensity, 0.0)
                self.assertEqual(plan.rendered_text, text)
                self.assertEqual(plan.protected_terms, [])


class SynthesisManifestTests(unittest.TestCase):
    PLAN_KEYS = {
        "source_text",
        "rendered_text",
        "language",
        "lexicon_id",
        "accent_pack_id",
        "accent_intensity",
        "replacements",
        "protected_terms",
    }

    def _run_synthesis(
        self,
        cfg: dict[str, object],
        text: str,
        destination: Path,
        failure: Exception | None = None,
    ) -> dict[str, object]:
        synth = (
            patch.object(core.SYNTHESIS_BACKEND, "synth_segments", side_effect=failure)
            if failure is not None
            else patch.object(
                core.SYNTHESIS_BACKEND,
                "synth_segments",
                lambda cfg, segments, semantic_token_cb=None: (
                    np.zeros(24000, dtype=np.float32),
                    24000,
                ),
            )
        )
        with patch.object(core.SYNTHESIS_BACKEND, "ensure"), synth, patch.object(
            core, "save_audio", return_value=destination / "line.wav"
        ), patch.object(core, "register_download", return_value="download"), patch.object(
            core, "append_history"
        ):
            return core.synthesize_lines(cfg, text, output_dir=destination)

    def _read_manifest(self, result: dict[str, object]) -> dict[str, object]:
        manifest_path = Path(str(result["pronunciation_manifest"]))
        self.assertTrue(manifest_path.is_file(), manifest_path)
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    def test_output_carries_the_full_plan_and_matches_rendered_text(self) -> None:
        text = "这是我的目的，但是我还是不是你的。"
        with tempfile.TemporaryDirectory() as directory:
            result = self._run_synthesis(synthesis_config("heavy"), text, Path(directory))

        self.assertEqual(result["completed"], 1)
        output = result["outputs"][0]
        pronunciation = output["pronunciation"]
        self.assertTrue(self.PLAN_KEYS.issubset(pronunciation), pronunciation)
        self.assertEqual(pronunciation["source_text"], text)
        self.assertEqual(pronunciation["rendered_text"], output["rendered_text"])
        self.assertEqual(output["rendered_text"], "这系我嘅目的，但是我还是唔系你嘅。")
        self.assertEqual(pronunciation["language"], "zh")
        self.assertEqual(pronunciation["lexicon_id"], "lex_zh_base_v1")
        self.assertEqual(pronunciation["accent_pack_id"], "guangpu_heavy")
        self.assertEqual(pronunciation["accent_intensity"], 1.0)
        self.assertEqual(output["accent_level"], "heavy")
        self.assertIn("但是", pronunciation["protected_terms"])
        self.assertTrue(pronunciation["replacements"])

    def test_heavy_protected_compounds_survive_in_the_synthesized_output(self) -> None:
        text = "但是我还是想去，因为这是目的。"
        with tempfile.TemporaryDirectory() as directory:
            result = self._run_synthesis(synthesis_config("heavy"), text, Path(directory))

        output = result["outputs"][0]
        self.assertEqual(output["rendered_text"], "但是我还是想去，因为这系目的。")
        for term in ("但是", "还是", "目的"):
            self.assertIn(term, output["rendered_text"])
        self.assertNotIn("但系", output["rendered_text"])
        self.assertNotIn("目嘅", output["rendered_text"])
        sources = [item["source"] for item in output["pronunciation"]["replacements"]]
        for protected in ("但是", "还是", "目的", "是的", "可是", "于是"):
            self.assertNotIn(protected, sources)

    def test_manifest_is_written_and_matches_every_output(self) -> None:
        text = "但是我还是想去。\n这是目的。"
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            result = self._run_synthesis(synthesis_config("heavy"), text, destination)
            manifest = self._read_manifest(result)

            self.assertEqual(result["pronunciation_manifest"], str(destination / "pronunciation.json"))
            self.assertEqual(result["pronunciation_manifest_error"], "")

            self.assertEqual(manifest["schema_version"], 1)
            self.assertTrue(manifest["generated_at"])
            self.assertEqual(manifest["accent_level"], "heavy")
            self.assertEqual(manifest["text_lang"], "zh")
            self.assertEqual(manifest["lexicon_id"], "lex_zh_base_v1")
            self.assertEqual(manifest["accent_pack_id"], "guangpu_heavy")
            self.assertEqual(manifest["accent_intensity"], 1.0)
            self.assertEqual(manifest["line_count"], 2)
            for term in ("目的", "的确", "但是", "还是"):
                self.assertIn(term, manifest["protected_terms"])

            self.assertEqual([line["index"] for line in manifest["lines"]], [1, 2])
            for line, output in zip(manifest["lines"], result["outputs"]):
                self.assertEqual(line["source_text"], output["text"])
                self.assertEqual(line["rendered_text"], output["rendered_text"])
                self.assertEqual(line["rendered_text"], output["pronunciation"]["rendered_text"])
                self.assertEqual(line["replacements"], output["pronunciation"]["replacements"])
                self.assertEqual(line["file"], output["file"])
                self.assertEqual(line["error"], "")

    def test_non_chinese_languages_write_a_none_lexicon_and_keep_text(self) -> None:
        for language, text in (("en", "This is a test."), ("ja", "これはテストです。")):
            with self.subTest(language=language), tempfile.TemporaryDirectory() as directory:
                result = self._run_synthesis(
                    synthesis_config("heavy", language), text, Path(directory)
                )
                output = result["outputs"][0]
                self.assertEqual(output["rendered_text"], text)
                self.assertEqual(output["pronunciation"]["rendered_text"], text)
                self.assertEqual(output["pronunciation"]["source_text"], text)
                self.assertEqual(output["pronunciation"]["language"], language)
                self.assertEqual(output["pronunciation"]["lexicon_id"], "none")
                self.assertEqual(output["pronunciation"]["accent_pack_id"], "none")
                self.assertEqual(output["pronunciation"]["accent_intensity"], 0.0)
                self.assertEqual(output["pronunciation"]["protected_terms"], [])

                manifest = self._read_manifest(result)
                self.assertEqual(manifest["lexicon_id"], "none")
                self.assertEqual(manifest["accent_pack_id"], "none")
                self.assertEqual(manifest["accent_intensity"], 0.0)
                self.assertEqual(manifest["protected_terms"], [])
                self.assertEqual(manifest["lines"][0]["rendered_text"], text)

    def test_mixed_line_plan_rebases_replacements_into_the_source_line(self) -> None:
        text = "<zh>是不是</zh><en>No.</en>"
        with tempfile.TemporaryDirectory() as directory:
            result = self._run_synthesis(synthesis_config("light"), text, Path(directory))

        output = result["outputs"][0]
        pronunciation = output["pronunciation"]
        self.assertEqual(output["rendered_text"], "系唔系 No.")
        self.assertEqual(pronunciation["rendered_text"], output["rendered_text"])
        self.assertEqual(pronunciation["language"], "mixed")
        self.assertTrue(pronunciation["replacements"])
        for item in pronunciation["replacements"]:
            self.assertEqual(text[item["start"] : item["end"]], item["source"])

    def test_failed_line_still_carries_a_serialisable_plan(self) -> None:
        text = "但是我还是想去。"
        with tempfile.TemporaryDirectory() as directory:
            result = self._run_synthesis(
                synthesis_config("heavy"), text, Path(directory), failure=RuntimeError("bad model")
            )
            manifest = self._read_manifest(result)

        output = result["outputs"][0]
        self.assertIn("RuntimeError", output["error"])
        self.assertEqual(output["file"], "")
        self.assertEqual(output["accent_level"], "heavy")
        self.assertEqual(output["rendered_text"], "但是我还是想去。")
        self.assertEqual(output["pronunciation"]["rendered_text"], output["rendered_text"])
        self.assertEqual(output["pronunciation"]["accent_pack_id"], "guangpu_heavy")
        json.dumps(output["pronunciation"], ensure_ascii=False)
        self.assertEqual(manifest["line_count"], 1)
        self.assertEqual(manifest["lines"][0]["error"], output["error"])
        self.assertEqual(manifest["lines"][0]["file"], "")

    def test_manifest_write_failure_is_reported_without_failing_synthesis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            with patch.object(
                core, "write_pronunciation_manifest", side_effect=OSError("disk full")
            ):
                result = self._run_synthesis(synthesis_config("heavy"), "你好。", destination)

        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["outputs"][0]["error"], "")
        self.assertEqual(result["pronunciation_manifest"], "")
        self.assertIn("OSError", result["pronunciation_manifest_error"])
        self.assertIn("disk full", result["pronunciation_manifest_error"])


class ReferencePairRouteTests(unittest.TestCase):
    def test_synthesis_rejects_a_known_mismatched_prompt(self) -> None:
        mismatch = {
            "status": "mismatched",
            "expected_text": "清单原文。",
            "actual_text": "错误文本。",
            "source": "sliced_corrected.list",
            "message": "参考文本与清单不匹配（CER 100.0%）。",
            "cer": 1.0,
        }
        core.SYNTHESIS_BACKEND._warmup["status"] = "ready"
        with patch.object(deps, "apply_voice_selection") as select, patch.object(
            reference_prompt_module, "reference_pair_check", return_value=mismatch
        ):
            select.return_value.config = {
                "ref_audio": "known.wav",
                "ref_text": "错误文本。",
            }
            select.return_value.text = "你好。"
            client = TestClient(app_module.create_app())
            response = client.post("/api/synthesis", json={"text": "你好。"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("参考音频与参考文本不匹配", response.json()["detail"])
        self.assertIn("清单原文", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
