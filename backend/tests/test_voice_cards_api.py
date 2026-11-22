"""Route contracts for the versioned VoiceCard registry (list / create / activate)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import backend.app as app_module
from backend.api import deps
from backend.services import voice_assets
from backend.services.core import load_config


class VoiceCardRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = voice_assets.VoiceCardStore(Path(self.tmp.name))
        self.client = TestClient(app_module.create_app())
        patcher = mock.patch.object(deps, "VOICE_CARDS", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_create_then_list_then_activate(self) -> None:
        created = self.client.post(
            "/api/voice-cards",
            json={"display_name": "测试声线", "language": "zh"},
        )
        self.assertEqual(created.status_code, 200)
        card = created.json()["card"]
        self.assertEqual(card["display_name"], "测试声线")
        self.assertEqual(card["status"], "active")
        self.assertEqual(card["version_count"], 1)
        self.assertTrue(card["active_version"])

        listed = self.client.get("/api/voice-cards")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()["cards"]), 1)

        activated = self.client.post(f"/api/voice-cards/{card['voice_id']}/activate")
        self.assertEqual(activated.status_code, 200)
        self.assertIn("message", activated.json())
        # 激活把卡的快照写回配置
        self.assertIn("voice_card_id", activated.json())

    def test_get_unknown_card_is_404(self) -> None:
        self.assertEqual(self.client.get("/api/voice-cards/no_such_card").status_code, 404)

    def test_empty_display_name_is_rejected(self) -> None:
        # 空白名称在服务层被拒（VoiceAssetError → 400），而不是 pydantic 422
        response = self.client.post("/api/voice-cards", json={"display_name": "  "})
        self.assertEqual(response.status_code, 400)
        self.assertIn("display_name", response.json()["detail"])

    def test_activate_writes_reference_into_config(self) -> None:
        before = load_config()
        created = self.client.post(
            "/api/voice-cards",
            json={"display_name": "快照卡", "language": "zh"},
        )
        card = created.json()["card"]
        activated = self.client.post(f"/api/voice-cards/{card['voice_id']}/activate")
        self.assertEqual(activated.status_code, 200)
        after = load_config()
        for key in ("gpt_model", "sovits_model", "ref_audio", "ref_text"):
            self.assertEqual(after.get(key), before.get(key), f"未改动字段应保持一致：{key}")

    def test_v1_mirror_serves_the_same_surface(self) -> None:
        self.assertEqual(self.client.get("/v1/voice-cards").status_code, 200)

    def test_delete_removes_card_and_list_shrinks(self) -> None:
        card = self.client.post(
            "/api/voice-cards", json={"display_name": "待删卡", "language": "zh"}
        ).json()["card"]
        deleted = self.client.delete(f"/api/voice-cards/{card['voice_id']}")
        self.assertEqual(deleted.status_code, 200)
        self.assertIn("已删除", deleted.json()["message"])
        listed = self.client.get("/api/voice-cards")
        self.assertEqual(len(listed.json()["cards"]), 0)
        self.assertEqual(self.client.get(f"/api/voice-cards/{card['voice_id']}").status_code, 404)

    def test_delete_unknown_card_is_404(self) -> None:
        self.assertEqual(self.client.delete("/api/voice-cards/no_such_card").status_code, 404)

    def test_delete_card_in_use_is_rejected(self) -> None:
        """正在用于合成的卡必须先切换，防止配置引用被删卡。"""
        created = self.client.post(
            "/api/voice-cards", json={"display_name": "使用中", "language": "zh"}
        ).json()["card"]
        self.client.post(f"/api/voice-cards/{created['voice_id']}/activate")
        with mock.patch.object(
            app_module.core, "load_config", return_value={"voice_card_id": created["voice_id"]}
        ):
            response = self.client.delete(f"/api/voice-cards/{created['voice_id']}")
        self.assertEqual(response.status_code, 400)
        self.assertIn("正在用于合成", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
