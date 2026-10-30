"""Platform feature APIs: lexicon, batch/dialogue export, provenance, API keys."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import backend.app as app_module  # noqa: E402
from backend.services import provenance  # noqa: E402


def _set_env(**values: str | None):
    return mock.patch.dict(
        os.environ, {k: v for k, v in values.items() if v is not None}, clear=False
    )


class LexiconApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        lexicon_file = Path(self._tmp.name) / "lexicon.json"
        patcher = mock.patch(
            "backend.services.user_lexicon.lexicon_path", return_value=lexicon_file
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(app_module.create_app())

    def test_crud_roundtrip(self) -> None:
        self.assertEqual(self.client.get("/api/lexicon").json()["entries"], [])
        created = self.client.post(
            "/api/lexicon", json={"term": "重创", "replacement": "虫创", "note": "chóng chuàng"}
        )
        self.assertEqual(created.status_code, 200)
        self.assertIn("下次生成即生效", created.json()["message"])
        listed = self.client.get("/api/lexicon").json()["entries"]
        self.assertEqual(listed[0]["term"], "重创")
        deleted = self.client.delete("/api/lexicon", params={"term": "重创"})
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(self.client.get("/api/lexicon").json()["entries"], [])

    def test_invalid_entry_returns_400(self) -> None:
        response = self.client.post(
            "/api/lexicon", json={"term": "同", "replacement": "同"}
        )
        self.assertEqual(response.status_code, 400)

    def test_delete_unknown_returns_404(self) -> None:
        self.assertEqual(
            self.client.delete("/api/lexicon", params={"term": "不存在"}).status_code, 404
        )


class BatchExportApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out_dir = Path(self._tmp.name)
        first = self.out_dir / "001.wav"
        second = self.out_dir / "002.wav"
        for path, text in ((first, "a"), (second, "b")):
            sf.write(str(path), 0.1 * np.sin(np.linspace(0, 1000, 8000)), 16000)
        self.snapshot = {
            "job_id": "synth_test",
            "status": "done",
            "output_dir": str(self.out_dir),
            "outputs": [
                {"text": "第一句", "file": str(first), "duration": 0.5, "error": ""},
                {"text": "第二句", "file": str(second), "duration": 0.5, "error": ""},
            ],
        }
        self.client = TestClient(app_module.create_app())

    def test_export_produces_downloadable_artifacts(self) -> None:
        with mock.patch.object(
            app_module.SYNTHESIS_JOBS, "snapshot", return_value=self.snapshot
        ), mock.patch.object(app_module.core, "register_download", lambda p: "f" * 32):
            response = self.client.post("/api/batch/export", json={"job_id": "synth_test"})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertTrue(data["srt_url"].startswith("/api/files/"))
        self.assertTrue(data["full_url"].startswith("/api/files/"))
        self.assertTrue(Path(self.out_dir / "episode.srt").is_file())

    def test_blank_output_dir_never_writes_to_cwd(self) -> None:
        """Regression: Path("") is "." and truthy, so the old fallback never fired."""
        snapshot = dict(self.snapshot, output_dir="")
        before = Path.cwd() / "episode.srt"
        existed_before = before.exists()
        with mock.patch.object(
            app_module.SYNTHESIS_JOBS, "snapshot", return_value=snapshot
        ), mock.patch.object(app_module.core, "register_download", lambda p: "f" * 32):
            response = self.client.post("/api/batch/export", json={"job_id": "synth_test"})
        self.assertEqual(response.status_code, 200, response.text)
        # 落到第一句音频所在的目录，而不是服务进程的工作目录。
        self.assertTrue(Path(self.out_dir / "episode.srt").is_file())
        if not existed_before:
            self.assertFalse(before.exists())

    def test_unfinished_job_rejected(self) -> None:
        running = dict(self.snapshot, status="running")
        with mock.patch.object(app_module.SYNTHESIS_JOBS, "snapshot", return_value=running):
            response = self.client.post("/api/batch/export", json={"job_id": "synth_test"})
        self.assertEqual(response.status_code, 409)

    def test_unknown_job_404(self) -> None:
        with mock.patch.object(app_module.SYNTHESIS_JOBS, "snapshot", return_value=None):
            self.assertEqual(
                self.client.post("/api/batch/export", json={"job_id": "x"}).status_code, 404
            )


class DialogueApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app_module.create_app())

    def test_synthesis_start_validates_cards_and_publicises(self) -> None:
        fake_snapshot = {
            "job_id": "dlg_1",
            "status": "running",
            "outputs": [],
        }
        with mock.patch.object(
            app_module.VOICE_CARDS, "list_cards", return_value=[]
        ):
            response = self.client.post(
                "/api/dialogue/synthesis",
                json={"script": "小明：你好", "roles": {"小明": "vc_missing"}},
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn("vc_missing", response.json()["detail"])
        with mock.patch.object(
            app_module.dialogue.DIALOGUE_JOBS, "start", return_value=fake_snapshot
        ) as fake_start:
            response = self.client.post(
                "/api/dialogue/synthesis", json={"script": "旁白句", "roles": {}}
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["job_id"], "dlg_1")
            fake_start.assert_called_once()
            script_arg, roles_arg, _config = fake_start.call_args[0]
            self.assertEqual(script_arg, "旁白句")

    def test_status_unknown_404(self) -> None:
        with mock.patch.object(
            app_module.dialogue.DIALOGUE_JOBS, "snapshot", return_value=None
        ):
            self.assertEqual(
                self.client.get("/api/dialogue/status/dlg_x").status_code, 404
            )

    def test_outputs_hide_paths_and_add_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "001.wav"
            sf.write(str(audio), 0.1 * np.sin(np.linspace(0, 1000, 8000)), 16000)
            snapshot = {
                "job_id": "dlg_2",
                "status": "done",
                "outputs": [{"speaker": "小明", "line": "你好", "file": str(audio)}],
            }
            with mock.patch.object(
                app_module.dialogue.DIALOGUE_JOBS, "snapshot", return_value=snapshot
            ), mock.patch.object(app_module.core, "register_download", lambda p: "e" * 32):
                data = self.client.get("/api/dialogue/status/dlg_2").json()
            self.assertNotIn("file", data["outputs"][0])
            self.assertTrue(data["outputs"][0]["url"].startswith("/api/files/"))


class ProvenanceVerifyApiTests(unittest.TestCase):
    def test_verify_roundtrip_via_download_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "001.wav"
            sf.write(str(audio), 0.1 * np.sin(np.linspace(0, 1000, 8000)), 16000)
            with mock.patch.object(
                provenance, "PROVENANCE_KEY_FILE", Path(tmp) / "provenance.key"
            ):
                provenance.write_provenance(audio, "测试句", {"voice_card_id": "vc_x"})
                with mock.patch.object(
                    app_module.core, "resolve_download", return_value=audio
                ):
                    response = self.client_post_verify(download_id="a" * 32)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()["valid"])
                self.assertEqual(response.json()["manifest"]["voice_card_id"], "vc_x")

    def test_outside_path_rejected(self) -> None:
        response = self.client_post_verify(path="C:/Windows/win.ini")
        self.assertEqual(response.status_code, 403)

    @staticmethod
    def client_post_verify(**payload) -> object:
        client = TestClient(app_module.create_app())
        return client.post("/api/provenance/verify", json=payload)


class ApiKeyAdminApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.keys_file = str(Path(self._tmp.name) / "keys.json")

    def test_admin_flow_and_v1_auth(self) -> None:
        with _set_env(XIANGHUA_API_KEYS_FILE=self.keys_file):
            client = TestClient(app_module.create_app())
            created = client.post(
                "/api/api-keys",
                json={"tenant": "试点工作室", "daily_request_quota": 10, "rpm_limit": 5},
            )
            self.assertEqual(created.status_code, 200, created.text)
            token = created.json()["key"]["token"]
            self.assertTrue(token.startswith("xh_"))
            listed = client.get("/api/api-keys").json()["keys"]
            self.assertEqual(listed[0]["tenant"], "试点工作室")
            self.assertNotIn("key_hash", listed[0])

            self.assertEqual(client.get("/v1/health").status_code, 401)
            ok = client.get("/v1/health", headers={"X-API-Key": token})
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(ok.json(), {"status": "ok"})

            revoked = client.delete(f"/api/api-keys/{listed[0]['key_id']}")
            self.assertEqual(revoked.status_code, 204)
            self.assertEqual(
                client.get("/v1/health", headers={"X-API-Key": token}).status_code, 401
            )

    def test_not_configured_returns_409(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XIANGHUA_API_KEYS_FILE", None)
            client = TestClient(app_module.create_app())
            self.assertEqual(client.get("/api/api-keys").status_code, 409)


if __name__ == "__main__":
    unittest.main()
