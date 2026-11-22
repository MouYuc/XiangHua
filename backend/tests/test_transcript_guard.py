"""Guards around the workshop transcript endpoints.

Regression 2026-11-21: the workshop UI let a user hit 「保存并继续」 before the
transcript had loaded, POSTing zero rows and wiping ``sliced_corrected.list``.
Feature extraction then "succeeded" over zero rows and the eventual training
crashed inside GPT-SoVITS with ``ZeroDivisionError``. The save endpoint must
refuse to write an empty list while the Whisper list still has content.

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

from backend import app as app_module
from backend.services import core


def _write_whisper(root: Path, rows: list[str]) -> None:
    asr = root / "custom_data" / "asr"
    asr.mkdir(parents=True, exist_ok=True)
    (asr / "sliced_whisper.list").write_text("\n".join(rows) + "\n", encoding="utf-8")


class TranscriptGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "GPT_SoVITS").mkdir(parents=True, exist_ok=True)
        self._patcher = mock.patch.object(
            core, "load_config", return_value={"gpt_sovits_root": str(self.root)}
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.client = TestClient(app_module.create_app())

    def test_empty_rows_is_rejected_while_whisper_list_has_content(self) -> None:
        _write_whisper(self.root, ["G:/x/a.wav|sliced|ZH|第一句。"])
        response = self.client.put("/api/training/transcript", json={"rows": []})
        self.assertEqual(response.status_code, 400)
        self.assertIn("空清单", response.json()["detail"])
        corrected = self.root / "custom_data" / "asr" / "sliced_corrected.list"
        self.assertFalse(corrected.exists(), "被拒的保存不应碰旧清单")

    def test_non_empty_rows_are_saved(self) -> None:
        _write_whisper(self.root, ["G:/x/a.wav|sliced|ZH|第一句。"])
        response = self.client.put(
            "/api/training/transcript",
            json={"rows": [{"path": "G:/x/a.wav", "text": "第一句。", "language": "ZH", "spk": "sliced"}]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        corrected = self.root / "custom_data" / "asr" / "sliced_corrected.list"
        self.assertTrue(corrected.is_file())
        self.assertIn("第一句。", corrected.read_text(encoding="utf-8"))

    def test_empty_rows_allowed_when_whisper_list_is_blank(self) -> None:
        # Whisper 清单本身为空（例如识别全部失败）时，保存空清单是合法动作。
        _write_whisper(self.root, [])
        response = self.client.put("/api/training/transcript", json={"rows": []})
        self.assertEqual(response.status_code, 200, response.text)


if __name__ == "__main__":
    unittest.main()
