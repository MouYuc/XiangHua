"""⑤ 扫描启用时自动建档：同名实验卡追加版本，否则新建卡并绑定配置。

2026-11-21 用户拍板改设计：工作坊「完成」步以前只把引擎切到新权重，用户以为
训练完成会自动出现声线卡。现在 ``POST /training/models/select`` 在填入最新
训练模型的同时自动建档/追加版本。

No model loads and no GPU here.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient

import backend.api.routers.training as training_router
import backend.app as app_module
from backend.api import deps
from backend.services import voice_assets


class ModelsSelectAutoCardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "GPT_SoVITS").mkdir(parents=True, exist_ok=True)
        self.ref = self.root / "ref.wav"
        self.ref.write_bytes(b"RIFF")
        self.store = voice_assets.VoiceCardStore(self.root / "voice_cards")
        self.client = TestClient(app_module.create_app())
        # select_models 路由会 core.save_config：必须连保存一起 mock，
        # 否则被 mock 的临时目录配置会写进真实 .guangpu_webui/config.json
        # （2026-11-21 事故：测试曾把真实配置的 gpt_sovits_root 覆盖成临时目录）。
        self._saved: dict = {}
        for patcher in (
            mock.patch.object(deps, "VOICE_CARDS", self.store),
            mock.patch.object(
                app_module.core,
                "load_config",
                return_value={"gpt_sovits_root": str(self.root), "ref_audio": str(self.ref)},
            ),
            mock.patch.object(
                app_module.core,
                "save_config",
                side_effect=lambda cfg: (self._saved.update(cfg), cfg)[1],
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _patch_models(self, gpt: str, sovits: str, exp: str = "Hutao") -> None:
        self._find_patcher = mock.patch.object(
            training_router, "find_models", return_value={"gpt": gpt, "sovits": sovits}
        )
        self._find_patcher.start()
        self.addCleanup(self._find_patcher.stop)
        self._state_patcher = mock.patch.object(
            training_router, "load_train_state", return_value={"exp": exp, "s1_epochs": 15, "s2_epochs": 12}
        )
        self._state_patcher.start()
        self.addCleanup(self._state_patcher.stop)

    def test_first_select_creates_named_card_and_binds_config(self) -> None:
        self._patch_models("gpt/A.ckpt", "sovits/A.pth")
        response = self.client.post("/api/training/models/select")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("已自动创建声线卡", response.json()["message"])
        cards = self.store.list_cards()
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["display_name"], "Hutao")
        self.assertEqual(response.json()["config"]["voice_card_id"], cards[0]["voice_id"])

    def test_second_select_appends_version_instead_of_new_card(self) -> None:
        self._patch_models("gpt/A.ckpt", "sovits/A.pth")
        self.client.post("/api/training/models/select")
        self._patch_models("gpt/B.ckpt", "sovits/B.pth")
        response = self.client.post("/api/training/models/select")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("已追加版本", response.json()["message"])
        cards = self.store.list_cards()
        self.assertEqual(len(cards), 1)
        self.assertEqual(len(cards[0]["versions"]), 2)
        config = response.json()["config"]
        self.assertEqual(config["voice_card_id"], cards[0]["voice_id"])
        self.assertTrue(config["gpt_model"].endswith("B.ckpt"))


if __name__ == "__main__":
    unittest.main()
