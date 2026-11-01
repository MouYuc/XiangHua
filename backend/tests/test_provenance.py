"""Provenance sidecar (C2PA 风格 + HMAC 签名) 契约测试。"""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
import soundfile as sf

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services import provenance, voice_assets


def _write_sine(path: Path, seconds: float = 0.2, sample_rate: int = 32000) -> None:
    samples = np.linspace(0.0, seconds, int(sample_rate * seconds), endpoint=False)
    audio = (0.2 * np.sin(2 * np.pi * 440.0 * samples)).astype(np.float32)
    sf.write(str(path), audio, sample_rate)


class ServerKeyTests(unittest.TestCase):
    def test_key_is_created_once_and_reloaded_identically(self) -> None:
        with TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "nested" / "provenance.key"
            first = provenance.get_or_create_server_key(key_path)
            self.assertEqual(len(first), 32)
            self.assertTrue(key_path.is_file())
            # 文件内容是 hex 文本，复读得到同一把 key
            self.assertEqual(
                bytes.fromhex(key_path.read_text(encoding="utf-8").strip()), first
            )
            self.assertEqual(provenance.get_or_create_server_key(key_path), first)


class ProvenanceRoundtripTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.audio = self.dir / "line_001.wav"
        _write_sine(self.audio)
        self.key_path = self.dir / "provenance.key"
        # 不碰真实 .guangpu_webui：默认 key 文件指向临时目录
        patcher = patch.object(provenance, "PROVENANCE_KEY_FILE", self.key_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _cfg(**overrides):
        base = {
            "voice_card_id": "vc_test",
            "voice_card_version": "2026-10-25.1",
            "accent_level": "off",
            "text_lang": "zh",
        }
        base.update(overrides)
        return base

    def test_write_and_verify_roundtrip_with_real_wav(self) -> None:
        text = "今天天气真不错。"
        sidecar = provenance.write_provenance(self.audio, text, self._cfg())
        self.assertEqual(sidecar, self.dir / "line_001.provenance.json")
        self.assertTrue(sidecar.is_file())
        result = provenance.verify_provenance(self.audio)
        self.assertTrue(result["valid"], result["reason"])
        manifest = result["manifest"]
        self.assertEqual(manifest["tool"], "xianghua")
        self.assertEqual(manifest["schema_version"], provenance.PROVENANCE_SCHEMA_VERSION)
        self.assertEqual(manifest["signature_algorithm"], "hmac-sha256")
        self.assertEqual(manifest["file_name"], self.audio.name)
        self.assertEqual(
            manifest["file_sha256"], hashlib.sha256(self.audio.read_bytes()).hexdigest()
        )
        self.assertEqual(
            manifest["text_sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest()
        )
        self.assertEqual(manifest["file_bytes"], self.audio.stat().st_size)
        self.assertEqual(manifest["voice_card_id"], "vc_test")
        self.assertEqual(manifest["voice_card_version"], "2026-10-25.1")
        self.assertEqual(manifest["accent_level"], "off")
        self.assertEqual(manifest["text_lang"], "zh")

    def test_sign_manifest_signs_a_copy(self) -> None:
        manifest = provenance.build_manifest(self.audio, "台词", self._cfg())
        signed = provenance.sign_manifest(manifest, b"k" * 32)
        self.assertIn("signature", signed)
        self.assertEqual(signed["signature_algorithm"], "hmac-sha256")
        self.assertNotIn("signature", manifest)

    def test_tampered_audio_fails_with_file_reason(self) -> None:
        provenance.write_provenance(self.audio, "台词", self._cfg())
        with self.audio.open("ab") as handle:
            handle.write(b"\x00tamper")
        result = provenance.verify_provenance(self.audio)
        self.assertFalse(result["valid"])
        self.assertIn("文件", result["reason"])

    def test_tampered_manifest_fails_signature_check(self) -> None:
        sidecar = provenance.write_provenance(
            self.audio, "台词", self._cfg(), license_id="lic_1"
        )
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        payload["license_id"] = "lic_forged"
        sidecar.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = provenance.verify_provenance(self.audio)
        self.assertFalse(result["valid"])
        self.assertIn("签名", result["reason"])

    def test_missing_sidecar_reports_reason(self) -> None:
        result = provenance.verify_provenance(self.audio)
        self.assertFalse(result["valid"])
        self.assertIn("不存在", result["reason"])
        self.assertIsNone(result["manifest"])

    def test_card_lookup_fills_license_and_consent(self) -> None:
        cards_root = self.dir / "voice_cards"
        with patch.object(voice_assets, "DEFAULT_VOICE_CARDS_DIR", cards_root):
            card = voice_assets.VoiceCardStore().create(
                {"ref_audio": "ref.wav", "ref_text": "参考文本", "prompt_lang": "zh"},
                "测试卡",
                license_id="lic_42",
                consent_id="con_42",
            )
            sidecar = provenance.write_provenance(
                self.audio, "台词", self._cfg(voice_card_id=card["voice_id"])
            )
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(payload["license_id"], "lic_42")
        self.assertEqual(payload["consent_id"], "con_42")
        result = provenance.verify_provenance(self.audio)
        self.assertTrue(result["valid"], result["reason"])

    def test_missing_card_leaves_license_none_and_still_writes(self) -> None:
        sidecar = provenance.write_provenance(
            self.audio, "台词", self._cfg(voice_card_id="vc_missing")
        )
        self.assertTrue(sidecar.is_file())
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertIsNone(payload["license_id"])
        self.assertIsNone(payload["consent_id"])
        result = provenance.verify_provenance(self.audio)
        self.assertTrue(result["valid"], result["reason"])


if __name__ == "__main__":
    unittest.main()
