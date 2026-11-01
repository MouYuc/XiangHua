"""Dialogue orchestration: script parsing and per-speaker synthesis jobs."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services import dialogue

FAKE_CARD = {
    "voice_id": "vc_test",
    "display_name": "测试卡",
    "status": "active",
    "active_version": "v1",
    "versions": [{"version": "v1", "timbre": {"ref_audio": "r.wav"}, "defaults": {}}],
}


def _fake_synthesis_result(text: str) -> dict:
    return {
        "outputs": [
            {
                "text": text,
                "rendered_text": text,
                "file": f"out/{text}.mp3",
                "duration": 1.2,
                "error": "",
            }
        ],
        "total": 1,
        "completed": 1,
    }


class ParseDialogueScriptTests(unittest.TestCase):
    def test_full_and_half_width_colons(self) -> None:
        parsed = dialogue.parse_dialogue_script("小明：你好\n小红: 你也你好")
        self.assertEqual(
            parsed,
            [
                {"speaker": "小明", "line": "你好"},
                {"speaker": "小红", "line": "你也你好"},
            ],
        )

    def test_lines_without_colon_are_narration(self) -> None:
        parsed = dialogue.parse_dialogue_script("夜色渐深。\n小明：谁在那儿？")
        self.assertEqual(parsed[0]["speaker"], dialogue.DEFAULT_SPEAKER)
        self.assertEqual(parsed[1]["speaker"], "小明")

    def test_comments_and_blank_lines_skipped(self) -> None:
        parsed = dialogue.parse_dialogue_script("# 这是注释\n\n小明：台词\n   \n")
        self.assertEqual(parsed, [{"speaker": "小明", "line": "台词"}])

    def test_first_colon_wins_and_speaker_keeps_inner_spaces(self) -> None:
        parsed = dialogue.parse_dialogue_script("小明 说：时间：三点了")
        self.assertEqual(parsed[0]["speaker"], "小明 说")
        self.assertEqual(parsed[0]["line"], "时间：三点了")

    def test_empty_line_part_skipped(self) -> None:
        self.assertEqual(dialogue.parse_dialogue_script("小明："), [])
        self.assertEqual(dialogue.parse_dialogue_script(""), [])


class DialogueJobManagerTests(unittest.TestCase):
    def _run_job(self, script: str, side_effects=None, card_lookup=None) -> dict:
        manager = dialogue.DialogueJobManager(card_lookup=card_lookup)
        with mock.patch.object(dialogue.core, "synthesize_lines") as synth:
            if side_effects is not None:
                synth.side_effect = side_effects
            else:
                synth.side_effect = lambda cfg, text, output_dir=None: _fake_synthesis_result(text)
            snapshot = manager.start(
                script,
                role_bindings={"小明": "vc_test"},
                base_config={"text_lang": "zh", "ref_audio": "r.wav"},
            )
            job_id = snapshot["job_id"]
            # 后台线程是 daemon；轮询到终态（真实现里每句都是毫秒级 stub）。
            import time

            for _ in range(200):
                current = manager.snapshot(job_id)
                if current and current.get("status") in dialogue.TERMINAL_STATUSES:
                    return current
                time.sleep(0.02)
            raise AssertionError("dialogue job did not finish")

    def test_all_lines_synthesised_with_card_overlay(self) -> None:
        seen_configs = []

        def fake_synth(cfg, text, output_dir=None):
            seen_configs.append(dict(cfg))
            return _fake_synthesis_result(text)

        manager = dialogue.DialogueJobManager(card_lookup=lambda vid: FAKE_CARD)
        with mock.patch.object(dialogue.core, "synthesize_lines", side_effect=fake_synth):
            snapshot = manager.start(
                "小明：第一句\n旁白：旁白句\n",
                role_bindings={"小明": "vc_test"},
                base_config={"text_lang": "zh"},
            )
            job_id = snapshot["job_id"]
            import time

            for _ in range(200):
                current = manager.snapshot(job_id)
                if current and current.get("status") in dialogue.TERMINAL_STATUSES:
                    snapshot = current
                    break
                time.sleep(0.02)

        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["completed"], 2)
        speakers = [item["speaker"] for item in snapshot["outputs"]]
        self.assertEqual(speakers, ["小明", "旁白"])
        # 绑定角色的句子带卡覆盖（voice_card_id 写回 cfg），旁白用原配置。
        self.assertEqual(seen_configs[0].get("voice_card_id"), "vc_test")
        self.assertNotIn("voice_card_id", seen_configs[1])

    def test_job_owns_one_output_dir(self) -> None:
        """Regression: each line used to get its own timestamped folder."""
        seen_dirs = []

        def fake_synth(cfg, text, output_dir=None):
            seen_dirs.append(output_dir)
            return _fake_synthesis_result(text)

        manager = dialogue.DialogueJobManager(card_lookup=lambda vid: FAKE_CARD)
        with mock.patch.object(dialogue.core, "synthesize_lines", side_effect=fake_synth):
            snapshot = manager.start(
                "小明：第一句\n小明：第二句\n",
                role_bindings={"小明": "vc_test"},
                base_config={"text_lang": "zh"},
            )
            job_id = snapshot["job_id"]
            import time

            for _ in range(200):
                current = manager.snapshot(job_id)
                if current and current.get("status") in dialogue.TERMINAL_STATUSES:
                    snapshot = current
                    break
                time.sleep(0.02)

        self.assertEqual(snapshot["status"], "done")
        self.assertTrue(snapshot["output_dir"])
        self.assertEqual(len({str(item) for item in seen_dirs}), 1)
        self.assertEqual(str(seen_dirs[0]), snapshot["output_dir"])

    def test_broken_line_does_not_abort_episode(self) -> None:
        def flaky(cfg, text, output_dir=None):
            if text == "坏句":
                raise RuntimeError("引擎拒绝")
            return _fake_synthesis_result(text)

        snapshot = self._run_job_with_impl(flaky)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["completed"], 1)
        failed_rows = [item for item in snapshot["outputs"] if not item["file"]]
        self.assertEqual(len(failed_rows), 1)
        self.assertIn("RuntimeError", failed_rows[0]["error"])

    def _run_job_with_impl(self, impl) -> dict:
        manager = dialogue.DialogueJobManager(card_lookup=lambda vid: FAKE_CARD)
        import time

        with mock.patch.object(dialogue.core, "synthesize_lines", side_effect=impl):
            snapshot = manager.start(
                "小明：好句\n小明：坏句\n",
                role_bindings={"小明": "vc_test"},
                base_config={"text_lang": "zh"},
            )
            job_id = snapshot["job_id"]
            for _ in range(200):
                current = manager.snapshot(job_id)
                if current and current.get("status") in dialogue.TERMINAL_STATUSES:
                    return current
                time.sleep(0.02)
        raise AssertionError("dialogue job did not finish")

    def test_missing_card_fails_only_that_line(self) -> None:
        def broken_lookup(voice_id: str) -> dict:
            raise KeyError(voice_id)

        snapshot = self._run_job_with_impl_and_lookup(
            lambda cfg, text, output_dir=None: _fake_synthesis_result(text), broken_lookup
        )
        self.assertEqual(snapshot["completed"], 0)
        self.assertTrue(all("KeyError" in item["error"] for item in snapshot["outputs"]))

    def _run_job_with_impl_and_lookup(self, impl, lookup) -> dict:
        manager = dialogue.DialogueJobManager(card_lookup=lookup)
        import time

        with mock.patch.object(dialogue.core, "synthesize_lines", side_effect=impl):
            snapshot = manager.start(
                "小明：台词\n", role_bindings={"小明": "vc_missing"}, base_config={"text_lang": "zh"}
            )
            job_id = snapshot["job_id"]
            for _ in range(200):
                current = manager.snapshot(job_id)
                if current and current.get("status") in dialogue.TERMINAL_STATUSES:
                    return current
                time.sleep(0.02)
        raise AssertionError("dialogue job did not finish")

    def test_empty_script_rejected(self) -> None:
        manager = dialogue.DialogueJobManager()
        with self.assertRaises(ValueError):
            manager.start("# 只有注释\n", role_bindings={}, base_config={})

    def test_snapshot_has_revision(self) -> None:
        self._run_job("小明：一句\n")
        # 通过 _run_job 已到终态；revision 至少递增过一次由 status/done 更新保证。
        # 这里直接验证 snapshot 的结构字段。
        manager = dialogue.DialogueJobManager(card_lookup=lambda vid: FAKE_CARD)
        with mock.patch.object(
            dialogue.core,
            "synthesize_lines",
            side_effect=lambda cfg, text, output_dir=None: _fake_synthesis_result(text),
        ):
            snapshot = manager.start("小明：另一句\n", {}, {"text_lang": "zh"})
            self.assertIn("revision", snapshot)
            self.assertGreaterEqual(snapshot["revision"], 0)


if __name__ == "__main__":
    unittest.main()
