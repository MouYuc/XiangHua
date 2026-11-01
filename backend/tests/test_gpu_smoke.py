"""End-to-end smoke test against the real engine and GPU.

Everything else in this suite runs on CPU with the model mocked, which is what
makes it fast enough to run on every save. The cost is that a whole class of
failure — wrong weights, a broken reference clip, a CUDA OOM at a realistic
top_k — is invisible to CI. This module closes that gap *opt-in*:

    XIANGHUA_GPU_TESTS=1 .venv\\Scripts\\python.exe -m pytest backend/tests/test_gpu_smoke.py -v

It reads the user's real configuration (model paths, reference audio) but never
writes to it: every artifact lands in a temporary directory. Skipped entirely
unless ``XIANGHUA_GPU_TESTS=1``, so the default suite and CI stay CPU-only.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

OPT_IN = os.environ.get("XIANGHUA_GPU_TESTS") == "1"


@pytest.mark.gpu
@unittest.skipUnless(OPT_IN, "需要真实引擎与显卡：设置 XIANGHUA_GPU_TESTS=1 后运行")
class GpuSynthesisSmokeTests(unittest.TestCase):
    """One short line through the whole path: config → engine → wav → readback."""

    @classmethod
    def setUpClass(cls) -> None:
        from backend.services import core

        cls.core = core
        cls.config = core.load_config()
        missing = [
            field
            for field in ("gpt_sovits_root", "gpt_model", "sovits_model", "ref_audio")
            if not str(cls.config.get(field) or "").strip()
        ]
        if missing:
            raise unittest.SkipTest(f"配置缺少引擎字段：{', '.join(missing)}")
        for field in ("gpt_model", "sovits_model", "ref_audio"):
            if not Path(str(cls.config[field])).is_file():
                raise unittest.SkipTest(f"{field} 指向的文件不存在：{cls.config[field]}")

    def _synthesize(self, text: str) -> dict:
        from backend.services import core

        with tempfile.TemporaryDirectory(prefix="gpu_smoke_") as raw:
            return core.synthesize_lines(self.config, text, Path(raw), should_stop=None)

    def test_one_line_is_synthesised_and_audible(self) -> None:
        text = "今天天气真不错，我们出去走走吧。"
        result = self._synthesize(text)

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["completed"], 1, result.get("outputs"))
        output = result["outputs"][0]
        self.assertEqual(output.get("error", ""), "")

        # The temp directory is created by the helper and is gone by the time we
        # assert, so check the metadata the API reports rather than stat-ing it.
        self.assertGreater(float(output.get("duration") or 0.0), 0.5)

    def test_output_is_not_a_collapsed_take(self) -> None:
        """A take far shorter than the text needs means the model returned junk."""
        from backend.services import core

        text = "这条路走到头，左转就到了。"
        expected = core.expected_segment_duration(text, "zh", float(self.config.get("speed", 1)))
        with tempfile.TemporaryDirectory(prefix="gpu_smoke_") as raw:
            result = core.synthesize_lines(self.config, text, Path(raw))
            produced = float(result["outputs"][0].get("duration") or 0.0)
        # `_short_output_floor` is the same rule the pipeline uses to reject a take.
        floor = core._short_output_floor(expected, text)
        self.assertGreater(produced, floor, f"疑似塌缩：{produced:.2f}s ≤ {floor:.2f}s")

    def test_repeated_calls_keep_producing_usable_audio(self) -> None:
        """Two calls in one warmed process must both be usable.

        Byte-identical output is **not** asserted here on purpose: measured on
        2026-11-01, two identical requests in the same process differ, while two
        *fresh processes* with the same seed are byte-identical (see
        ``scripts/seed_determinism_check.py``). The reproducibility claim in the
        project memory is a per-process claim, and asserting it in-process would
        encode a guarantee the engine does not provide.
        """
        durations = []
        for _ in range(2):
            with tempfile.TemporaryDirectory(prefix="gpu_smoke_") as raw:
                result = self.core.synthesize_lines(self.config, "测试稳定性。", Path(raw))
                durations.append(float(result["outputs"][0].get("duration") or 0.0))
        for value in durations:
            self.assertGreater(value, 0.3)
        # A different take length is fine; a half-length collapse is not.
        self.assertLess(abs(durations[0] - durations[1]), max(durations) * 0.5)


if __name__ == "__main__":
    unittest.main()
