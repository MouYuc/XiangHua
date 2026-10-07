import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import SYNTHESIS_JOBS, create_app
from backend.schemas import ConfigUpdate
from backend.services import core
from backend.services import voice_selection
from backend.services.voice_selection import (
    AccentPack,
    apply_voice_selection,
    load_accent_packs,
    rewrite_text_for_accent,
)


ACCENT_MANIFEST = {
    "version": 1,
    "packs": [
        {
            "id": "standard",
            "display_name": "原音色",
            "lang": "zh",
            "description": "保留当前声音档案的参考音频。",
            "uses_profile_reference": True,
            "enabled": True,
        },
        {
            "id": "zh-dongbei",
            "display_name": "东北普通话",
            "lang": "zh",
            "description": "高表现力东北口吻。",
            "uses_profile_reference": False,
            "ref_audio": "reference.wav",
            "ref_text": "老铁稳住，这波我来收尾。",
            "prompt_lang": "zh",
            "pronunciation_lexicon": {"什么": "啥"},
            "sentence_templates": {
                "稳住，我来收尾。": "老铁稳住，这波我来收尾。"
            },
            "enabled": True,
        },
    ],
}


def _dongbei_accent() -> AccentPack:
    return AccentPack(
        id="zh-dongbei",
        display_name="东北普通话",
        lang="zh",
        description="",
        uses_profile_reference=False,
        ref_audio="reference.wav",
        ref_text="老铁稳住，这波我来收尾。",
        prompt_lang="zh",
        pronunciation_lexicon={"什么": "啥"},
        sentence_templates={"稳住，我来收尾。": "老铁稳住，这波我来收尾。"},
        enabled=True,
        available=True,
    )


class VoiceSelectionSchemaTests(unittest.TestCase):
    def test_config_update_accepts_profile_and_accent_ids(self) -> None:
        update = ConfigUpdate(voice_profile_id="current", accent_id="standard")
        self.assertEqual(update.voice_profile_id, "current")
        self.assertEqual(update.accent_id, "standard")

    def test_default_config_uses_current_voice_and_standard_accent(self) -> None:
        config = core.default_config()
        self.assertEqual(config["voice_profile_id"], "current")
        self.assertEqual(config["accent_id"], "standard")


class AccentCatalogTests(unittest.TestCase):
    def test_loads_and_marks_missing_reference_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "accent_packs.json"
            manifest_path.write_text(
                json.dumps(ACCENT_MANIFEST, ensure_ascii=False), encoding="utf-8"
            )
            packs = load_accent_packs(manifest_path=manifest_path, assets_root=root)

        by_id = {item.id: item for item in packs}
        self.assertTrue(by_id["standard"].available)
        self.assertFalse(by_id["zh-dongbei"].available)

    def test_rewrites_plain_text_with_sentence_template(self) -> None:
        rewritten = rewrite_text_for_accent("稳住，我来收尾。", _dongbei_accent())
        self.assertEqual(rewritten, "老铁稳住，这波我来收尾。")

    def test_rewrites_only_chinese_and_cantonese_tag_bodies(self) -> None:
        rewritten = rewrite_text_for_accent(
            "<zh>这是什么</zh><en>AI Voice</en>", _dongbei_accent()
        )
        self.assertEqual(rewritten, "<zh>这是啥</zh><en>AI Voice</en>")


class ApplyVoiceSelectionTests(unittest.TestCase):
    def test_uses_current_profile_and_accent_reference_before_synthesis(self) -> None:
        config = {
            "voice_profile_id": "current",
            "accent_id": "standard",
            "gpt_model": "gpt.ckpt",
            "sovits_model": "sovits.pth",
            "ref_audio": "profile.wav",
            "ref_text": "当前音色。",
            "prompt_lang": "zh",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reference.wav").write_bytes(b"fake audio")
            manifest_path = root / "accent_packs.json"
            manifest_path.write_text(
                json.dumps(ACCENT_MANIFEST, ensure_ascii=False), encoding="utf-8"
            )
            prepared = apply_voice_selection(
                config,
                "稳住，我来收尾。",
                voice_profile_id="current",
                accent_id="zh-dongbei",
                manifest_path=manifest_path,
                assets_root=root,
            )
        self.assertEqual(prepared.text, "老铁稳住，这波我来收尾。")
        self.assertEqual(prepared.config["ref_audio"], str(root / "reference.wav"))
        self.assertEqual(prepared.config["prompt_lang"], "zh")


class VoiceSelectionRouteTests(unittest.TestCase):
    def test_synthesis_route_applies_selection_before_jobs_start(self) -> None:
        captured: dict[str, object] = {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reference.wav").write_bytes(b"fake audio")
            manifest_path = root / "accent_packs.json"
            manifest_path.write_text(
                json.dumps(ACCENT_MANIFEST, ensure_ascii=False), encoding="utf-8"
            )
            config = core.default_config() | {
                "gpt_model": "gpt.ckpt",
                "sovits_model": "sovits.pth",
                "ref_audio": "profile.wav",
                "ref_text": "当前音色。",
            }

            def fake_start(
                cfg: dict[str, object], text: str, output_dir: object = None
            ) -> dict[str, object]:
                captured["cfg"] = cfg
                captured["text"] = text
                return {
                    "job_id": "test-job",
                    "status": "running",
                    "phase": "prepare",
                    "current": 0,
                    "total": 1,
                    "completed": 0,
                    "current_text": "",
                    "rendered_text": "",
                    "output_dir": "",
                    "outputs": [],
                    "error": "",
                }

            with (
                patch.object(core, "load_config", return_value=config),
                patch.object(
                    core.SYNTHESIS_BACKEND,
                    "warmup_status",
                    return_value={"status": "ready", "message": "", "error": ""},
                ),
                patch.object(voice_selection, "ACCENT_MANIFEST_FILE", manifest_path),
                patch.object(voice_selection, "ACCENTS_ROOT", root),
                patch.object(SYNTHESIS_JOBS, "start", fake_start),
            ):
                client = TestClient(create_app())
                response = client.post(
                    "/api/synthesis",
                    json={
                        "text": "稳住，我来收尾。",
                        "voice_profile_id": "current",
                        "accent_id": "zh-dongbei",
                    },
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["job_id"], "test-job")
            self.assertEqual(captured["text"], "老铁稳住，这波我来收尾。")
            self.assertEqual(captured["cfg"]["ref_audio"], str(root / "reference.wav"))

    def test_voice_options_route_lists_profiles_and_accents(self) -> None:
        with patch.object(core, "load_config", return_value=core.default_config()):
            client = TestClient(create_app())
            response = client.get("/api/voice/options")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("profiles", data)
        self.assertIn("accents", data)
        self.assertEqual(data["profiles"][0]["id"], "current")


if __name__ == "__main__":
    unittest.main()
