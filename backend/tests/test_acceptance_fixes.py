"""Regression tests for the defects found during the 2026-11-01 acceptance run.

1. ``cmd_feat`` executed ``GPT_SoVITS/prepare_datasets/`` (a directory) instead of
   ``.../1-get-text.py``, so ``POST /training/features`` died with
   "can't find '__main__' module".
2. ``cmd_train`` returned ``_ensure_s2_logs`` itself, which needs ``root``; ``start_job``
   calls ``pre()`` with no arguments, so ``POST /training/run`` failed before s1.
3. Multpart filenames were latin-1 decoded, so Chinese uploads landed as mojibake.
4. A one-character line (「噗！」) was judged by readback and flagged as a collapse.

No model loads and no GPU here.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services import core  # noqa: E402
from tools import train  # noqa: E402


class TrainingCommandTests(unittest.TestCase):
    def test_feature_extraction_runs_script_files_not_the_directory(self) -> None:
        commands = train.cmd_feat(r"G:\Clone-youself\GPT-SoVITS", "exp1")
        self.assertEqual(len(commands), 4)
        for cmd, _cwd, _env in commands:
            script = cmd[-1]
            self.assertTrue(script.endswith(".py"), script)
            self.assertIn("GPT_SoVITS/prepare_datasets/", script)
            self.assertFalse(script.endswith("/"), script)
        self.assertEqual(
            [cmd[-1].rsplit("/", 1)[-1] for cmd, _cwd, _env in commands],
            ["1-get-text.py", "2-get-hubert-wav32k.py", "2-get-sv.py", "3-get-semantic.py"],
        )

    def test_train_pre_hook_takes_no_arguments(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _cmds, pre = train.cmd_train(str(root), "exp1")
            pre()  # start_job calls it exactly like this
            self.assertTrue((root / "custom_data" / "dataset" / "logs_s2_v2Pro").is_dir())


class UploadFilenameTests(unittest.TestCase):
    def test_latin1_mojibake_is_recovered(self) -> None:
        from backend.app import _upload_filename

        decoded_by_starlette = "胡桃.mp4".encode("utf-8").decode("latin-1")
        self.assertEqual(_upload_filename(SimpleNamespace(filename=decoded_by_starlette)), "胡桃.mp4")

    def test_real_utf8_name_is_kept(self) -> None:
        from backend.app import _upload_filename

        self.assertEqual(_upload_filename(SimpleNamespace(filename="胡桃.mp4")), "胡桃.mp4")

    def test_missing_filename_uses_fallback(self) -> None:
        from backend.app import _upload_filename

        self.assertEqual(_upload_filename(SimpleNamespace(filename=None), "media"), "media")


class SharedWhisperCacheTests(unittest.TestCase):
    def test_media_and_reference_paths_share_one_model(self) -> None:
        """Two private lru_caches put two large-v3 copies on the card."""
        from backend.services import media_ingest, voice_ingest, whisper_cache

        calls = []
        sentinel = object()

        def fake_load(model_name):
            calls.append(model_name)
            return sentinel

        original = whisper_cache.load_whisper
        whisper_cache.load_whisper = fake_load
        try:
            self.assertIs(media_ingest._load_whisper("large-v3"), sentinel)
            self.assertIs(voice_ingest._load_whisper("large-v3"), sentinel)
        finally:
            whisper_cache.load_whisper = original
        self.assertEqual(calls, ["large-v3", "large-v3"])

    def test_shim_env_keeps_children_importable(self) -> None:
        """Children import tools/my_utils.py, which imports gradio at module scope."""
        import os

        from tools.train import _child_env

        env = _child_env({"inp_text": "x.list"})
        parts = env["PYTHONPATH"].split(os.pathsep)
        self.assertTrue(any(part.endswith("_shim_boot") for part in parts))
        self.assertEqual(env["inp_text"], "x.list")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")


def _cfg(**overrides):
    base = {
        "gpt_model": "gpt.ckpt",
        "sovits_model": "sovits.pth",
        "ref_audio": "ref.wav",
        "ref_text": "参考。",
        "text_lang": "zh",
        "prompt_lang": "zh",
        "accent_level": "off",
        "auto_lightmid": False,
        "speed": 1.0,
        "temperature": 0.95,
        "top_k": 30,
        "top_p": 0.88,
        "sample_steps": 64,
        "pause_second": 0.15,
        "seed": 20261017,
        "take_selection": True,
    }
    base.update(overrides)
    return base


class _CountingJudge:
    def __init__(self, cer: float) -> None:
        self.cer_value = cer
        self.calls = 0

    def cer(self, audio_path, reference_text):
        self.calls += 1
        return self.cer_value


class ShortLineTests(unittest.TestCase):
    def _run(self, text: str, seconds: float, judge):
        calls = []

        def fake_synth(take_cfg, segments, semantic_token_cb=None):
            calls.append(dict(take_cfg))
            return np.full(int(seconds * 32000), 0.05, dtype=np.float32), 32000

        backend = SimpleNamespace(
            ensure=lambda config: None,
            synth_segments=fake_synth,
            _inference_calls=SimpleNamespace(count=1),
        )
        with TemporaryDirectory() as tmp:
            with patch.object(core, "SYNTHESIS_BACKEND", backend), patch.object(
                core, "READBACK_JUDGE", judge
            ), patch.object(core, "append_history", lambda entries: None):
                result = core.synthesize_lines(_cfg(), text, output_dir=Path(tmp))
        return result["outputs"][0], calls

    def test_single_character_line_skips_readback_and_collapse_flag(self) -> None:
        judge = _CountingJudge(2.0)
        output, calls = self._run("噗！", 0.36, judge)
        self.assertEqual(len(calls), 1)
        self.assertEqual(judge.calls, 0)  # readback CER is meaningless on one char
        self.assertFalse(output["suspiciously_short"])

    def test_collapsed_single_character_take_is_still_flagged(self) -> None:
        judge = _CountingJudge(2.0)
        output, _calls = self._run("噗！", 0.05, judge)
        self.assertTrue(output["suspiciously_short"])

    def test_normal_line_still_judged_and_retried(self) -> None:
        judge = _CountingJudge(0.9)
        output, calls = self._run("今天天气真不错。", 3.0, judge)
        self.assertEqual(judge.calls, core._TAKE_SELECTION_MAX_TAKES)
        self.assertEqual(len(calls), core._TAKE_SELECTION_MAX_TAKES)
        self.assertEqual(output["takes"], core._TAKE_SELECTION_MAX_TAKES)


if __name__ == "__main__":
    unittest.main()
