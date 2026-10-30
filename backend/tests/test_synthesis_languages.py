import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app import _merge_config, create_app
from backend.schemas import ConfigUpdate
from backend.services import core


class _FakePipeline:
    def __init__(self):
        self.request = None

    def run(self, request):
        self.request = request
        yield 24000, [0.0]


class SynthesisLanguageTests(unittest.TestCase):
    def test_schema_accepts_the_five_supported_languages(self):
        for language in ("zh", "yue", "en", "ja", "ko"):
            config = ConfigUpdate(text_lang=language, prompt_lang=language)
            self.assertEqual(config.text_lang, language)
            self.assertEqual(config.prompt_lang, language)

    def test_schema_rejects_unknown_languages(self):
        with self.assertRaises(ValidationError):
            ConfigUpdate(text_lang="de")
        with self.assertRaises(ValidationError):
            ConfigUpdate(prompt_lang="fr")

    def test_default_config_uses_chinese_for_both_languages(self):
        config = core.default_config()
        self.assertEqual(config["text_lang"], "zh")
        self.assertEqual(config["prompt_lang"], "zh")

    def test_default_config_leaves_mixed_eos_controls_dynamic(self):
        config = core.default_config()
        self.assertIsNone(config["mixed_eos_floor"])
        self.assertIsNone(config["mixed_token_cap"])

    def test_mixed_control_null_clears_saved_override(self):
        with patch.object(
            core,
            "load_config",
            return_value={"mixed_eos_floor": 11, "mixed_token_cap": 240},
        ):
            merged = _merge_config(ConfigUpdate(mixed_eos_floor=None, mixed_token_cap=None))
        self.assertIsNone(merged["mixed_eos_floor"])
        self.assertIsNone(merged["mixed_token_cap"])

    def test_synth_one_passes_independent_languages_to_the_pipeline(self):
        pipeline = _FakePipeline()
        backend = core.SynthesisBackend()
        backend._pipeline = pipeline
        with tempfile.TemporaryDirectory() as root:
            backend.synth_one({
                "gpt_sovits_root": root,
                "text_lang": "en",
                "prompt_lang": "ja",
                "ref_audio": "reference.wav",
                "ref_text": "こんにちは",
                "top_k": 30, "top_p": 0.88, "temperature": 0.95,
                "speed": 1.0, "pause_second": 0.15, "sample_steps": 64,
            }, "Hello")
        self.assertEqual(pipeline.request["text_lang"], "en")
        self.assertEqual(pipeline.request["prompt_lang"], "ja")

    def test_only_chinese_text_is_lightmid_converted(self):
        config = {"auto_lightmid": True, "text_lang": "zh"}
        self.assertEqual(core.render_synthesis_text("不是", config), "唔系")
        self.assertEqual(
            core.render_synthesis_text("不是", {**config, "text_lang": "yue"}),
            "不是",
        )

    def test_reference_audio_uses_a_generated_name_inside_managed_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(core, "REFERENCE_AUDIO_DIR", Path(directory)):
                saved = core.save_reference_audio("../../voice.wav", io.BytesIO(b"audio"))
                self.assertEqual(saved.suffix, ".wav")
                self.assertNotEqual(saved.name, "voice.wav")
                self.assertEqual(saved.read_bytes(), b"audio")

    def test_reference_audio_rejects_unsupported_or_oversize_uploads(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(core, "REFERENCE_AUDIO_DIR", Path(directory)):
                with self.assertRaisesRegex(ValueError, "支持"):
                    core.save_reference_audio("voice.txt", io.BytesIO(b"audio"))
                with patch.object(core, "MAX_REFERENCE_AUDIO_BYTES", 3):
                    with self.assertRaisesRegex(ValueError, "100 MB"):
                        core.save_reference_audio("voice.wav", io.BytesIO(b"1234"))


class ReferenceAudioRouteTests(unittest.TestCase):
    def test_upload_route_returns_the_saved_path(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(core, "REFERENCE_AUDIO_DIR", Path(directory)):
                client = TestClient(create_app())
                response = client.post(
                    "/api/reference-audio",
                    data={"auto_prepare": "false"},
                    files={"audio": ("prompt.ogg", b"audio", "audio/ogg")},
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["path"].endswith(".ogg"))
                self.assertTrue(Path(response.json()["path"]).is_file())

    def test_upload_route_rejects_unsupported_and_oversize_files(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(core, "REFERENCE_AUDIO_DIR", Path(directory)):
                client = TestClient(create_app())
                response = client.post(
                    "/api/reference-audio",
                    files={"audio": ("prompt.txt", b"not audio", "text/plain")},
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("支持", response.json()["detail"])

                with patch.object(core, "MAX_REFERENCE_AUDIO_BYTES", 3):
                    response = client.post(
                        "/api/reference-audio",
                        files={"audio": ("prompt.wav", b"1234", "audio/wav")},
                    )
                self.assertEqual(response.status_code, 400)
                self.assertIn("100 MB", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
