"""VoiceCard / StylePack contract tests for the versioned voice-asset layer."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import backend.app as app_module
from backend.api import deps
from backend.services import voice_assets


def _config(**overrides):
    base = {
        "gpt_model": "GPT_SoVITS/pretrained_models/s1v3.ckpt",
        "sovits_model": "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth",
        "ref_audio": "reference.wav",
        "ref_text": "今天天气真不错，我们出去走走吧。",
        "prompt_lang": "zh",
        "accent_level": "off",
        "speed": 1.0,
        "pause_second": 0.15,
    }
    base.update(overrides)
    return base


class StylePackTests(unittest.TestCase):
    def test_shipped_packs_load_and_natural_exists(self) -> None:
        packs = voice_assets.load_style_packs()
        by_id = {pack.style_id: pack for pack in packs}
        self.assertIn("natural", by_id)
        self.assertIn("calm", by_id)
        for pack in packs:
            self.assertTrue(pack.display_name)
            for key in pack.synthesis:
                self.assertIn(key, {"speed", "pause_second"})

    def test_unknown_style_pack_is_a_400_error(self) -> None:
        with self.assertRaises(voice_assets.StylePackNotFoundError):
            voice_assets.resolve_style_pack("nope")

    def test_apply_respects_explicit_request_fields(self) -> None:
        calm = voice_assets.resolve_style_pack("calm")
        updated = voice_assets.apply_style_pack(
            _config(speed=1.2), calm, explicit_fields={"speed"}
        )
        self.assertEqual(updated["speed"], 1.2)
        self.assertEqual(updated["pause_second"], 0.3)
        self.assertEqual(updated["style_id"], "calm")


class VoiceCardStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = voice_assets.VoiceCardStore(self.root)

    def test_create_writes_card_json_with_version_one(self) -> None:
        card = self.store.create(_config(), "我的声线", license_id="lic_001")
        self.assertEqual(len(card["versions"]), 1)
        self.assertEqual(card["active_version"], card["versions"][0]["version"])
        self.assertTrue(card["active_version"].endswith(".1"))
        self.assertEqual(card["versions"][0]["timbre"]["ref_audio"], "reference.wav")
        self.assertEqual(card["versions"][0]["defaults"]["accent_level"], "off")
        self.assertEqual(card["versions"][0]["defaults"]["lexicon_id"], "lex_zh_base_v1")
        self.assertEqual(card["license_id"], "lic_001")
        path = self.root / card["voice_id"] / "card.json"
        self.assertTrue(path.is_file())
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["voice_id"], card["voice_id"])

    def test_create_requires_reference_audio(self) -> None:
        with self.assertRaises(voice_assets.VoiceAssetError):
            self.store.create(_config(ref_audio=""), "空的")

    def test_versions_are_append_only_and_history_is_frozen(self) -> None:
        card = self.store.create(_config(), "我的声线")
        v1 = copy.deepcopy(card["versions"][0])
        card = self.store.add_version(
            card["voice_id"], _config(ref_audio="new.wav", accent_level="light")
        )
        self.assertEqual(len(card["versions"]), 2)
        self.assertEqual(card["active_version"], v1["version"].replace(".1", ".2"))
        self.assertEqual(card["versions"][0], v1)
        self.assertEqual(card["versions"][1]["timbre"]["ref_audio"], "new.wav")
        self.assertEqual(card["versions"][1]["defaults"]["accent_level"], "light")

    def test_update_metadata_and_rollback_to_old_version(self) -> None:
        card = self.store.create(_config(), "我的声线")
        v1 = card["active_version"]
        card = self.store.add_version(card["voice_id"], _config(ref_audio="new.wav"))
        card = self.store.update(card["voice_id"], active_version=v1, status="retired")
        self.assertEqual(card["active_version"], v1)
        self.assertEqual(card["status"], "retired")
        with self.assertRaises(voice_assets.VoiceAssetError):
            self.store.update(card["voice_id"], active_version="2099-01-01.9")

    def test_unknown_card_is_404(self) -> None:
        with self.assertRaises(voice_assets.VoiceCardNotFoundError):
            self.store.get("vc_missing")

    def test_corrupt_card_does_not_hide_the_rest(self) -> None:
        good = self.store.create(_config(), "好的")
        bad_dir = self.root / "vc_broken"
        bad_dir.mkdir()
        (bad_dir / "card.json").write_text("not-json", encoding="utf-8")
        cards = self.store.list_cards()
        ids = [card["voice_id"] for card in cards]
        self.assertIn(good["voice_id"], ids)
        self.assertIn("vc_broken", ids)
        broken = next(card for card in cards if card["voice_id"] == "vc_broken")
        self.assertTrue(broken.get("corrupt"))


class ApplyVoiceCardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = voice_assets.VoiceCardStore(Path(self._tmp.name))
        self.card = self.store.create(
            _config(ref_audio="card_ref.wav", ref_text="卡片参考。", accent_level="medium"),
            "我的声线",
        )

    def test_overlays_timbre_and_defaults(self) -> None:
        updated = voice_assets.apply_voice_card(_config(), self.card)
        self.assertEqual(updated["ref_audio"], "card_ref.wav")
        self.assertEqual(updated["ref_text"], "卡片参考。")
        self.assertEqual(updated["accent_level"], "medium")
        self.assertEqual(updated["voice_card_id"], self.card["voice_id"])
        self.assertEqual(updated["voice_card_version"], self.card["active_version"])
        # medium defaults don't pin a style; natural keeps engine defaults.
        self.assertEqual(updated["speed"], 1.0)

    def test_explicit_request_fields_beat_card_defaults(self) -> None:
        updated = voice_assets.apply_voice_card(
            _config(accent_level="heavy"), self.card, explicit_fields={"accent_level"}
        )
        self.assertEqual(updated["accent_level"], "heavy")

    def test_retired_card_refuses_new_synthesis(self) -> None:
        retired = self.store.update(self.card["voice_id"], status="retired")
        with self.assertRaises(voice_assets.VoiceAssetError):
            voice_assets.apply_voice_card(_config(), retired)

    def test_style_from_card_applies_synthesis_knobs(self) -> None:
        card = self.store.create(_config(style_id="calm"), "风格卡")
        updated = voice_assets.apply_voice_card(_config(), card)
        self.assertEqual(updated["pause_second"], 0.3)
        self.assertEqual(updated["speed"], 0.95)


class VoiceCardRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app_module.create_app())
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = voice_assets.VoiceCardStore(Path(self._tmp.name))
        self._patchers = [
            patch.object(deps, "VOICE_CARDS", self.store),
            patch.object(
                app_module.core,
                "load_config",
                lambda: _config(ref_audio="prepared/ref.wav", ref_text="参考。"),
            ),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_full_card_lifecycle_over_api(self) -> None:
        created = self.client.post(
            "/api/voice-cards", json={"display_name": "我的声线", "license_id": "lic_001"}
        )
        self.assertEqual(created.status_code, 200)
        card = created.json()["card"]
        self.assertEqual(card["version_count"], 1)
        self.assertTrue(card["timbre_available"] is False)  # path is fake by design

        listed = self.client.get("/api/voice-cards").json()["cards"]
        self.assertEqual([c["voice_id"] for c in listed], [card["voice_id"]])

        got = self.client.get(f"/api/voice-cards/{card['voice_id']}").json()["card"]
        self.assertEqual(got["license_id"], "lic_001")

        patched = self.client.patch(
            f"/api/voice-cards/{card['voice_id']}", json={"status": "retired"}
        )
        self.assertEqual(patched.json()["card"]["status"], "retired")

        reactivated = self.client.patch(
            f"/api/voice-cards/{card['voice_id']}", json={"status": "active"}
        )
        self.assertEqual(reactivated.json()["card"]["status"], "active")

        versioned = self.client.post(
            f"/api/voice-cards/{card['voice_id']}/versions", json={"notes": "重录后"}
        )
        self.assertEqual(versioned.status_code, 200)
        self.assertEqual(versioned.json()["card"]["version_count"], 2)

    def test_unknown_card_routes_are_404(self) -> None:
        self.assertEqual(self.client.get("/api/voice-cards/vc_none").status_code, 404)
        self.assertEqual(
            self.client.patch("/api/voice-cards/vc_none", json={"status": "retired"}).status_code,
            404,
        )

    def test_create_without_reference_audio_is_400(self) -> None:
        with patch.object(app_module.core, "load_config", lambda: _config(ref_audio="")):
            response = self.client.post("/api/voice-cards", json={"display_name": "空"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("参考音频", response.json()["detail"])

    def test_activate_writes_active_version_into_saved_config(self) -> None:
        card = self.client.post("/api/voice-cards", json={"display_name": "我的声线"}).json()["card"]
        saved: dict = {}
        # The saved config drifts after the card exists; activating must restore
        # the card's frozen version, not the current values.
        drifted = _config(ref_audio="drifted.wav", ref_text="漂移了。")
        with patch.object(app_module.core, "load_config", lambda: drifted), patch.object(
            app_module.core, "save_config", lambda cfg: saved.update(cfg) or cfg
        ):
            response = self.client.post(f"/api/voice-cards/{card['voice_id']}/activate")
        self.assertEqual(response.status_code, 200)
        self.assertIn("ref_audio", response.json()["applied"])
        self.assertEqual(saved["ref_audio"], "prepared/ref.wav")
        self.assertEqual(saved["ref_text"], "参考。")
        self.assertEqual(saved["voice_card_id"], card["voice_id"])

    def test_voice_options_exposes_styles(self) -> None:
        styles = self.client.get("/api/voice/options").json()["styles"]
        self.assertTrue(any(style["style_id"] == "natural" for style in styles))

    def test_synthesis_applies_card_before_starting_the_job(self) -> None:
        card = self.client.post("/api/voice-cards", json={"display_name": "我的声线"}).json()["card"]
        captured: dict = {}

        def fake_start(config, text):
            captured.update(config=config, text=text)
            return {
                "job_id": "synth_fake",
                "status": "queued",
                "phase": "queued",
                "progress": 0,
                "total": 1,
                "completed": 0,
                "outputs": [],
                "created_at": "",
                "updated_at": "",
                "error": "",
            }

        backend = SimpleNamespace(warmup_status=lambda: {"status": "ready", "message": ""})
        with patch.object(app_module.core, "SYNTHESIS_BACKEND", backend), patch.object(
            app_module.SYNTHESIS_JOBS, "start", fake_start
        ):
            response = self.client.post(
                "/api/synthesis",
                json={"text": "测试。", "voice_card_id": card["voice_id"]},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["config"]["ref_audio"], "prepared/ref.wav")
        self.assertEqual(captured["config"]["voice_card_id"], card["voice_id"])

    def test_synthesis_with_unknown_card_is_404(self) -> None:
        backend = SimpleNamespace(warmup_status=lambda: {"status": "ready", "message": ""})
        with patch.object(app_module.core, "SYNTHESIS_BACKEND", backend):
            response = self.client.post(
                "/api/synthesis", json={"text": "测试。", "voice_card_id": "vc_none"}
            )
        self.assertEqual(response.status_code, 404)

    def test_retired_card_cannot_synthesize(self) -> None:
        card = self.client.post("/api/voice-cards", json={"display_name": "我的声线"}).json()["card"]
        self.client.patch(f"/api/voice-cards/{card['voice_id']}", json={"status": "retired"})
        backend = SimpleNamespace(warmup_status=lambda: {"status": "ready", "message": ""})
        with patch.object(app_module.core, "SYNTHESIS_BACKEND", backend):
            response = self.client.post(
                "/api/synthesis", json={"text": "测试。", "voice_card_id": card["voice_id"]}
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("停用", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
