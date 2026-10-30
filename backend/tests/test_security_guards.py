"""Security guards: host/origin checks, /v1 API key, path confinement, the
synthesis queue middleware, and the SPA traversal fix.

Each test here corresponds to a finding from the 2026-10-25 red-team review:
the SPA catch-all served ``/%2e%2e/%2e%2e/backend/app.py``, ``inspect`` and
``timbre/convert-file`` accepted arbitrary filesystem paths, ``voice_id`` was
joined into a directory path unvalidated, and state-changing endpoints ran
with no origin or API-key checks.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import backend.app as app_module  # noqa: E402
from backend.services import core, media_ingest  # noqa: E402


def _set_env(**values: str | None):
    return mock.patch.dict(os.environ, {k: v for k, v in values.items() if v is not None}, clear=False)


class HostGuardTests(unittest.TestCase):
    def test_foreign_host_is_rejected(self) -> None:
        client = TestClient(app_module.create_app(), base_url="http://evil.example")
        response = client.get("/api/health")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "FORBIDDEN_HOST")

    def test_remote_hosts_flag_disables_the_check(self) -> None:
        with _set_env(XIANGHUA_ALLOW_REMOTE_HOSTS="1"):
            client = TestClient(app_module.create_app(), base_url="http://evil.example")
            self.assertEqual(client.get("/api/health").status_code, 200)

    def test_loopback_host_is_allowed(self) -> None:
        client = TestClient(app_module.create_app(), base_url="http://127.0.0.1")
        self.assertEqual(client.get("/api/health").status_code, 200)


class OriginGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app_module.create_app())

    def test_foreign_origin_is_rejected(self) -> None:
        response = self.client.get("/api/health", headers={"Origin": "http://evil.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "FORBIDDEN_ORIGIN")

    def test_loopback_origin_is_allowed(self) -> None:
        for origin in ("http://localhost:5173", "http://127.0.0.1:7860"):
            response = self.client.get("/api/health", headers={"Origin": origin})
            self.assertEqual(response.status_code, 200, origin)

    def test_absent_origin_is_allowed(self) -> None:
        self.assertEqual(self.client.get("/api/health").status_code, 200)


class V1ApiKeyTests(unittest.TestCase):
    def test_v1_requires_key_when_configured(self) -> None:
        with _set_env(XIANGHUA_API_KEY="secret-key"):
            client = TestClient(app_module.create_app())
            self.assertEqual(client.get("/v1/health").status_code, 401)
            self.assertEqual(
                client.get("/v1/health", headers={"X-API-Key": "wrong"}).status_code, 401
            )
            ok = client.get("/v1/health", headers={"X-API-Key": "secret-key"})
            self.assertEqual(ok.status_code, 200)
            bearer = client.get("/v1/health", headers={"Authorization": "Bearer secret-key"})
            self.assertEqual(bearer.status_code, 200)

    def test_api_prefix_stays_keyless_and_v1_open_without_env(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XIANGHUA_API_KEY", None)
            client = TestClient(app_module.create_app())
            self.assertEqual(client.get("/api/health").status_code, 200)
            self.assertEqual(client.get("/v1/health").status_code, 200)


class SpaTraversalTests(unittest.TestCase):
    """The catch-all must never serve files outside the frontend dist."""

    @classmethod
    def setUpClass(cls) -> None:
        import tempfile

        cls.tmp = Path(tempfile.mkdtemp(prefix="xianghua_dist_"))
        (cls.tmp / "index.html").write_text("<html>app</html>", encoding="utf-8")
        assets = cls.tmp / "assets"
        assets.mkdir()
        (assets / "app.js").write_text("console.log(1)", encoding="utf-8")
        # 一个 dist 外的诱饵文件：穿越成功就能读到它。
        cls.decoy = cls.tmp.parent / (cls.tmp.name + "_secret.txt")
        cls.decoy.write_text("SECRET", encoding="utf-8")
        with mock.patch.dict(
            os.environ, {"XIANGHUA_FRONTEND_DIST": str(cls.tmp)}, clear=False
        ):
            cls.client = TestClient(app_module.create_app())

    @classmethod
    def tearDownClass(cls) -> None:
        import shutil

        cls.decoy.unlink(missing_ok=True)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_encoded_dotdot_traversal_is_blocked(self) -> None:
        for path in (
            "/%2e%2e/%2e%2e/" + self.decoy.name,
            "/..%2F..%2F" + self.decoy.name,
            "/%2e%2e/" + self.decoy.name,
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertNotIn("SECRET", response.text, path)
            self.assertIn("app", response.text, "should fall back to index.html")

    def test_regular_assets_still_served(self) -> None:
        response = self.client.get("/assets/app.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("console.log", response.text)


class PathConfinementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app_module.create_app())

    def test_inspect_rejects_paths_outside_the_repo(self) -> None:
        for payload in (
            {"path": "C:/Windows/win.ini"},
            {"directory": "C:/Windows"},
            {"path": "\\\\localhost\\c$\\Windows\\win.ini"},
        ):
            response = self.client.post("/api/reference-audio/inspect", json=payload)
            self.assertIn(response.status_code, (400, 403), payload)
            self.assertNotEqual(response.json().get("code"), "INTERNAL_ERROR")

    def test_convert_file_rejects_paths_outside_the_repo(self) -> None:
        response = self.client.post(
            "/api/timbre/convert-file", json={"source_path": "C:/Windows/win.ini"}
        )
        self.assertEqual(response.status_code, 403)

    def test_environment_select_rejects_unknown_roots(self) -> None:
        response = self.client.post(
            "/api/environment/select", json={"path": "C:/Windows"}
        )
        self.assertIn(response.status_code, (400, 403))

    def test_extra_roots_env_extends_the_allowlist(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "clip.wav"
            outside.write_bytes(b"")
            with _set_env(XIANGHUA_ALLOWED_PATH_ROOTS=tmp):
                response = self.client.post(
                    "/api/reference-audio/inspect", json={"path": str(outside)}
                )
                # 圈禁通过（不再是 403）；文件不是有效音频，报业务错误即可。
                self.assertNotEqual(response.status_code, 403)


class VoiceIdValidationTests(unittest.TestCase):
    def test_store_rejects_separator_ids(self) -> None:
        from backend.services.voice_assets import VoiceCardNotFoundError, VoiceCardStore

        store = VoiceCardStore()
        for bad in ("..\\voice_cards\\x", "../x", "a/b", "a:b", "", ".", ".."):
            with self.assertRaises(VoiceCardNotFoundError, msg=repr(bad)):
                store.get(bad)

    def test_api_rejects_traversal_voice_id(self) -> None:
        client = TestClient(app_module.create_app())
        response = client.get("/api/voice-cards/..%5Cvoice_cards%5Cx")
        self.assertEqual(response.status_code, 404)


class PronunciationPatchSwitchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app_module.create_app())

    def _upload(self, text: str) -> object:
        return self.client.post(
            "/api/engine/pronunciation-patch",
            files={"patch": ("p.patch", text.encode("utf-8"), "text/plain")},
        )

    def test_disabled_by_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XIANGHUA_ALLOW_PRONUNCIATION_PATCH", None)
            response = self._upload("diff --git a/GPT_SoVITS/text/chinese.py b/GPT_SoVITS/text/chinese.py\nGP_PINYIN\n")
            self.assertEqual(response.status_code, 403)

    def test_enabled_but_out_of_scope_patch_is_rejected(self) -> None:
        with _set_env(XIANGHUA_ALLOW_PRONUNCIATION_PATCH="1"):
            patch_text = (
                "diff --git a/GPT_SoVITS/text/chinese.py b/GPT_SoVITS/text/chinese.py\n"
                "GP_PINYIN\n"
                "diff --git a/GPT_SoVITS/TTS_infer_pack/TTS.py b/GPT_SoVITS/TTS_infer_pack/TTS.py\n"
            )
            response = self._upload(patch_text)
            self.assertEqual(response.status_code, 400)
            self.assertIn("GPT_SoVITS/text", response.json()["detail"])


class MediaUploadCapTests(unittest.TestCase):
    def test_oversized_upload_is_rejected_and_cleaned(self) -> None:
        import io
        import shutil
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            service = media_ingest.MediaIngestService(Path(tmp))
            session = service.create_session("cap-test", rights_declared=True, rights_note="test")
            directory = service.session_dir(session["session_id"])
            with _set_env(XIANGHUA_MAX_MEDIA_BYTES=str(1024 * 1024)):
                stream = io.BytesIO(b"x" * (2 * 1024 * 1024))
                with self.assertRaises(media_ingest.MediaIngestError):
                    service.ingest_file(session["session_id"], "big.wav", stream)
            source_dir = directory / "source"
            leftovers = list(source_dir.glob("*")) if source_dir.is_dir() else []
            self.assertEqual(leftovers, [], "超限上传的分片文件必须被清理")
            shutil.rmtree(directory, ignore_errors=True)


class SynthesisQueueMiddlewareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app_module.create_app())

    def test_queue_full_returns_429(self) -> None:
        from backend.services.synthesis_jobs import SYNTHESIS_JOBS

        with mock.patch.object(SYNTHESIS_JOBS, "active_count", return_value=8):
            response = self.client.post("/api/synthesis", json={"text": "排队测试。"})
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.json()["code"], "RATE_LIMITED")

    def test_oversized_synthesis_body_returns_413(self) -> None:
        with _set_env(XIANGHUA_SYNTHESIS_MAX_BODY_BYTES="1024"):
            response = self.client.post(
                "/api/synthesis", json={"text": "长" * 2048}
            )
            self.assertEqual(response.status_code, 413)
            self.assertEqual(response.json()["code"], "PAYLOAD_TOO_LARGE")


class PatchTargetParsingTests(unittest.TestCase):
    def test_targets_extracted_from_diff_headers(self) -> None:
        text = (
            "diff --git a/GPT_SoVITS/text/chinese.py b/GPT_SoVITS/text/chinese.py\n"
            "--- a/GPT_SoVITS/text/chinese.py\n"
            "+++ b/GPT_SoVITS/text/chinese.py\n"
            "rename to GPT_SoVITS/text/other.py\n"
        )
        self.assertEqual(
            set(core._patch_targets(text)),
            {"GPT_SoVITS/text/chinese.py", "GPT_SoVITS/text/other.py"},
        )


if __name__ == "__main__":
    unittest.main()
