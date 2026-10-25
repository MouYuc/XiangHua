"""Unit tests for reference-prompt lookup and pairing validation.

Run from the repository root:

    .venv\\Scripts\\python.exe -m unittest backend.tests.test_reference_prompt -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services import reference_prompt as rp  # noqa: E402


def write_manifest(root: Path, name: str, rows: list[tuple[str, str, str]]) -> Path:
    path = root / name
    lines = [f"{audio}|sliced|{language}|{text}\n" for audio, language, text in rows]
    path.write_text("".join(lines), encoding="utf-8")
    return path


class ReferencePromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.asr_dir = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_exact_path_returns_prompt_and_metadata(self) -> None:
        audio = str(self.asr_dir / "sliced" / "训练.wav_0000018880_0000143680.wav")
        write_manifest(
            self.asr_dir,
            "sliced_corrected.list",
            [(audio, "ZH", "今天天气真不错，我们出去走走吧。")],
        )

        result = rp.lookup_reference_prompt(audio, asr_dir=self.asr_dir)

        self.assertEqual(
            result,
            {
                "path": audio,
                "text": "今天天气真不错，我们出去走走吧。",
                "source": "sliced_corrected.list",
                "language": "ZH",
            },
        )

    def test_punctuation_and_whitespace_differences_are_matched(self) -> None:
        audio = r"D:\slices\prompt.wav"
        write_manifest(
            self.asr_dir,
            "sliced_corrected.list",
            [(audio, "ZH", "今天天气真不错，我们出去走走吧。")],
        )

        result = rp.reference_pair_check(
            audio,
            " 今天 天气真不错 我们出去走走吧！ ",
            asr_dir=self.asr_dir,
        )

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["cer"], 0.0)
        self.assertEqual(result["expected_text"], "今天天气真不错，我们出去走走吧。")
        self.assertEqual(result["source"], "sliced_corrected.list")
        self.assertIn("一致", result["message"])

    def test_a_small_transcription_error_is_tolerated(self) -> None:
        audio = r"D:\slices\prompt.wav"
        write_manifest(
            self.asr_dir,
            "sliced_corrected.list",
            [(audio, "ZH", "今天天气真不错，我们出去走走吧。")],
        )

        result = rp.reference_pair_check(
            audio,
            "今天天气真错，我们出去走走吧。",
            asr_dir=self.asr_dir,
        )

        self.assertEqual(result["status"], "matched")
        self.assertLessEqual(result["cer"], rp.MAX_CER)
        self.assertGreater(result["cer"], 0.0)
        self.assertIn("基本一致", result["message"])

    def test_an_obvious_mismatch_is_rejected(self) -> None:
        audio = r"D:\slices\prompt.wav"
        write_manifest(
            self.asr_dir,
            "sliced_corrected.list",
            [(audio, "ZH", "今天天气真不错，我们出去走走吧。")],
        )

        result = rp.reference_pair_check(
            audio,
            "这条路走到头，左转就到了。",
            asr_dir=self.asr_dir,
        )

        self.assertEqual(result["status"], "mismatched")
        self.assertGreater(result["cer"], rp.MAX_CER)
        self.assertIn("不匹配", result["message"])

    def test_unknown_audio_is_unknown_not_an_error(self) -> None:
        write_manifest(
            self.asr_dir,
            "sliced_corrected.list",
            [(r"D:\slices\known.wav", "ZH", "已知文本。")],
        )

        result = rp.reference_pair_check(
            r"D:\slices\missing.wav",
            "随便写点什么。",
            asr_dir=self.asr_dir,
        )

        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["expected_text"])
        self.assertIsNone(result["source"])
        self.assertIn("未在参考音清单中找到", result["message"])

    def test_a_missing_manifest_directory_is_quietly_unknown(self) -> None:
        result = rp.lookup_reference_prompt(
            r"D:\slices\prompt.wav",
            asr_dir=self.asr_dir / "does-not-exist",
        )

        self.assertIsNone(result)

    def test_corrected_manifest_takes_priority(self) -> None:
        audio = r"D:\slices\prompt.wav"
        write_manifest(
            self.asr_dir,
            "sliced_corrected.list",
            [(audio, "ZH", "这是人工校正后的文本。")],
        )
        write_manifest(
            self.asr_dir,
            "sliced_whisper.list",
            [(audio, "ZH", "这是自动识别文本。")],
        )
        write_manifest(
            self.asr_dir,
            "sliced.list",
            [(audio, "ZH", "这是原始清单文本。")],
        )

        result = rp.lookup_reference_prompt(audio, asr_dir=self.asr_dir)

        self.assertEqual(result["text"], "这是人工校正后的文本。")
        self.assertEqual(result["source"], "sliced_corrected.list")

    def test_filename_fallback_accepts_a_different_drive_or_root(self) -> None:
        manifest_audio = (
            r"G:\training\custom_data\sliced\训练.wav_0000018880_0000143680.wav"
        )
        queried_audio = (
            r"D:\reference_audio\sliced\训练.wav_0000018880_0000143680.wav"
        )
        write_manifest(
            self.asr_dir,
            "sliced_corrected.list",
            [(manifest_audio, "ZH", "今天天气真不错，我们出去走走吧。")],
        )

        result = rp.lookup_reference_prompt(queried_audio, asr_dir=self.asr_dir)

        self.assertIsNotNone(result)
        self.assertEqual(result["path"], manifest_audio)
        self.assertEqual(result["text"], "今天天气真不错，我们出去走走吧。")

    def test_path_matching_normalizes_separators_dots_and_case(self) -> None:
        manifest_audio = r"G:\training\custom_data\sliced\Prompt.WAV"
        queried_audio = r"g:/training/custom_data/./sliced/prompt.wav"
        write_manifest(
            self.asr_dir,
            "sliced_corrected.list",
            [(manifest_audio, "ZH", "大小写和分隔符不同。")],
        )

        result = rp.lookup_reference_prompt(queried_audio, asr_dir=self.asr_dir)

        self.assertIsNotNone(result)
        self.assertEqual(result["text"], "大小写和分隔符不同。")

    def test_blank_reference_text_is_empty(self) -> None:
        audio = r"D:\slices\prompt.wav"
        write_manifest(
            self.asr_dir,
            "sliced_corrected.list",
            [(audio, "ZH", "今天天气真不错。")],
        )

        result = rp.reference_pair_check(audio, " \t，。", asr_dir=self.asr_dir)

        self.assertEqual(result["status"], "empty")
        self.assertIsNone(result["cer"])
        self.assertIn("参考文本为空", result["message"])


if __name__ == "__main__":
    unittest.main()
