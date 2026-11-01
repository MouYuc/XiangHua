"""Path validation for the model/reference config fields.

``path_guard`` already confined every *request* path that reaches a filesystem
read; the configuration fields were the hole. These tests pin the policy,
including the two deliberate escapes (unchanged value = round-trip, scanner-found
engine root = allowed).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services import config_guard, path_guard


class ConfigGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _engine_tree(self, base: Path) -> Path:
        root = base / "engine"
        (root / "GPT_SoVITS" / "TTS_infer_pack").mkdir(parents=True)
        (root / "GPT_SoVITS" / "TTS_infer_pack" / "TTS.py").write_text("# stub", encoding="utf-8")
        return root

    def _wav(self, base: Path) -> Path:
        path = base / "ref.wav"
        path.write_bytes(b"RIFF")
        return path

    def test_unchanged_value_is_not_rechecked(self) -> None:
        """The UI autosaves the whole config object; a round-trip is not a new grant."""
        outside = self.tmp / "elsewhere.wav"
        outside.write_bytes(b"RIFF")
        current = {"ref_audio": str(outside)}
        # Would fail if it were a *change*; passing proves the round-trip escape.
        config_guard.validate_config_update({"ref_audio": str(outside)}, current)

    def test_reference_audio_outside_allowed_roots_is_rejected(self) -> None:
        outside = self._wav(self.tmp)
        with patch.object(path_guard, "allowed_roots", return_value=[REPO_ROOT.resolve()]):
            with self.assertRaises(config_guard.ConfigPathError) as caught:
                config_guard.validate_config_update({"ref_audio": str(outside)}, {})
        self.assertIn("XIANGHUA_ALLOWED_PATH_ROOTS", str(caught.exception))

    def test_reference_audio_inside_allowed_roots_is_accepted(self) -> None:
        inside = self._wav(self.tmp)
        with patch.object(path_guard, "allowed_roots", return_value=[self.tmp.resolve()]):
            config_guard.validate_config_update({"ref_audio": str(inside)}, {})

    def test_missing_reference_file_is_rejected(self) -> None:
        missing = self.tmp / "nope.wav"
        with self.assertRaises(config_guard.ConfigPathError) as caught:
            config_guard.validate_config_update({"ref_audio": str(missing)}, {})
        self.assertIn("不存在", str(caught.exception))

    def test_engine_root_without_the_tts_marker_is_rejected(self) -> None:
        empty = self.tmp / "not-an-engine"
        empty.mkdir()
        with self.assertRaises(config_guard.ConfigPathError) as caught:
            config_guard.validate_config_update({"gpt_sovits_root": str(empty)}, {})
        self.assertIn("GPT_SoVITS", str(caught.exception))

    def test_engine_root_found_by_the_scanner_is_accepted_outside_the_repo(self) -> None:
        root = self._engine_tree(self.tmp)
        with (
            patch.object(path_guard, "allowed_roots", return_value=[REPO_ROOT.resolve()]),
            patch.object(config_guard, "_scanned_roots", return_value={str(root.resolve())}),
        ):
            config_guard.validate_config_update({"gpt_sovits_root": str(root)}, {})

    def test_engine_root_outside_the_repo_and_not_scanned_is_rejected(self) -> None:
        root = self._engine_tree(self.tmp)
        with (
            patch.object(path_guard, "allowed_roots", return_value=[REPO_ROOT.resolve()]),
            patch.object(config_guard, "_scanned_roots", return_value=set()),
        ):
            with self.assertRaises(config_guard.ConfigPathError):
                config_guard.validate_config_update({"gpt_sovits_root": str(root)}, {})

    def test_model_inside_the_configured_engine_root_is_accepted(self) -> None:
        """Weights trained into an engine tree on another drive must stay usable.

        The docstring promised this allowance from the start, but the code only
        checked ``allowed_roots``: "填入最新模型" saved a new path and every later
        synthesis then failed with a 400 until the page was reloaded.
        """
        root = self._engine_tree(self.tmp)
        model = root / "GPT_weights_v2Pro" / "trained-e14.ckpt"
        model.parent.mkdir(parents=True)
        model.write_bytes(b"ckpt")
        with patch.object(path_guard, "allowed_roots", return_value=[REPO_ROOT.resolve()]):
            config_guard.validate_config_update(
                {"gpt_model": str(model)}, {"gpt_sovits_root": str(root)}
            )

    def test_model_outside_both_the_allowlist_and_the_engine_root_is_rejected(self) -> None:
        root = self._engine_tree(self.tmp)
        stray = self.tmp / "loose.ckpt"
        stray.write_bytes(b"ckpt")
        with patch.object(path_guard, "allowed_roots", return_value=[REPO_ROOT.resolve()]):
            with self.assertRaises(config_guard.ConfigPathError):
                config_guard.validate_config_update(
                    {"gpt_model": str(stray)}, {"gpt_sovits_root": str(root)}
                )

    def test_switching_the_engine_root_and_the_model_in_one_update_is_accepted(self) -> None:
        """The model is judged against the root this same payload adopts."""
        root = self._engine_tree(self.tmp)
        model = root / "GPT_weights_v2Pro" / "trained-e14.ckpt"
        model.parent.mkdir(parents=True)
        model.write_bytes(b"ckpt")
        with (
            patch.object(path_guard, "allowed_roots", return_value=[REPO_ROOT.resolve()]),
            patch.object(config_guard, "_scanned_roots", return_value={str(root.resolve())}),
        ):
            config_guard.validate_config_update(
                {"gpt_sovits_root": str(root), "gpt_model": str(model)}, {}
            )

    def test_empty_and_absent_fields_are_ignored(self) -> None:
        config_guard.validate_config_update({"ref_audio": ""}, {})
        config_guard.validate_config_update({"ref_audio": None}, {})
        config_guard.validate_config_update({"gpt_model": "  "}, {})
        config_guard.validate_config_update({"top_k": 30}, {})


if __name__ == "__main__":
    unittest.main()
