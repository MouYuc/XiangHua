"""Batch episode exports: SRT timestamps, ffmpeg concat, per-role stems."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services import batch_production


def _write_tone(path: Path, seconds: float = 0.4, rate: int = 16000) -> None:
    frames = int(rate * seconds)
    sf.write(str(path), 0.2 * np.sin(np.linspace(0, 440 * 2 * np.pi, frames)), rate)


class SrtTimestampTests(unittest.TestCase):
    def test_formats_hours_minutes_seconds_millis(self) -> None:
        self.assertEqual(batch_production.srt_timestamp(0), "00:00:00,000")
        self.assertEqual(batch_production.srt_timestamp(1.5), "00:00:01,500")
        self.assertEqual(batch_production.srt_timestamp(61.25), "00:01:01,250")
        self.assertEqual(batch_production.srt_timestamp(3661.0), "01:01:01,000")

    def test_negative_and_invalid_clamp_to_zero(self) -> None:
        self.assertEqual(batch_production.srt_timestamp(-3), "00:00:00,000")
        self.assertEqual(batch_production.srt_timestamp("bad"), "00:00:00,000")


class BuildSrtBlocksTests(unittest.TestCase):
    def test_cumulative_timeline_with_gap_and_speaker(self) -> None:
        srt = batch_production.build_srt_blocks(
            [
                {"text": "第一句", "duration": 2.0, "speaker": "小明"},
                {"text": "第二句", "duration": 1.5},
            ],
            gap_seconds=0.5,
        )
        blocks = srt.strip().split("\n\n")
        self.assertEqual(len(blocks), 2)
        first_lines = blocks[0].splitlines()
        self.assertEqual(first_lines[0], "1")
        self.assertEqual(first_lines[1], "00:00:00,000 --> 00:00:02,000")
        self.assertEqual(first_lines[2], "【小明】第一句")
        second_lines = blocks[1].splitlines()
        # 第二句在 2.0 + gap 0.5 处开始
        self.assertEqual(second_lines[1], "00:00:02,500 --> 00:00:04,000")

    def test_blank_text_entries_are_skipped(self) -> None:
        srt = batch_production.build_srt_blocks(
            [{"text": "  ", "duration": 1.0}, {"text": "有效", "duration": 1.0}]
        )
        self.assertEqual(srt.strip().split("\n\n")[-1].splitlines()[0], "1")


class ExportEpisodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.audio = self.dir / "audio"
        self.audio.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def _outputs(self) -> list[dict]:
        first = self.audio / "001.wav"
        second = self.audio / "002.wav"
        _write_tone(first)
        _write_tone(second)
        return [
            {"text": "第一句", "duration": 0.4, "file": str(first)},
            {"text": "第二句", "duration": 0.4, "file": str(second)},
            {"text": "丢失句", "duration": 0.4, "file": str(self.audio / "missing.wav")},
        ]

    def test_export_episode_writes_srt_and_full_track(self) -> None:
        result = batch_production.export_episode(self._outputs(), self.dir)
        self.assertEqual(result["line_count"], 2)
        self.assertEqual(result["failed"], 1)
        self.assertTrue(Path(result["srt"]).is_file())
        content = Path(result["srt"]).read_text(encoding="utf-8")
        self.assertIn("第一句", content)
        self.assertNotIn("丢失句", content)
        self.assertTrue(Path(result["full"]).is_file())
        self.assertGreater(Path(result["full"]).stat().st_size, 1000)

    def test_stems_grouped_by_speaker(self) -> None:
        outputs = self._outputs()
        speakers = {0: "小明", 1: "小红"}
        result = batch_production.export_episode(outputs, self.dir, speakers=speakers)
        self.assertEqual(set(result["stems"]), {"小明", "小红"})
        for stem in result["stems"].values():
            self.assertTrue(Path(stem).is_file(), stem)

    def test_empty_outputs_produce_srt_only(self) -> None:
        result = batch_production.export_episode([], self.dir)
        self.assertEqual(result["full"], "")
        self.assertEqual(result["line_count"], 0)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg 不在 PATH")
    def test_concat_audio_produces_longer_file(self) -> None:
        first = self.audio / "a.wav"
        second = self.audio / "b.wav"
        _write_tone(first, 0.4)
        _write_tone(second, 0.4)
        target = self.dir / "joined.wav"
        batch_production.concat_audio([first, second], target)
        merged = sf.info(str(target))
        self.assertAlmostEqual(merged.duration, 0.8, delta=0.05)

    def test_concat_empty_list_raises(self) -> None:
        with self.assertRaises(batch_production.BatchExportError):
            batch_production.concat_audio([], self.dir / "x.wav")


if __name__ == "__main__":
    unittest.main()
