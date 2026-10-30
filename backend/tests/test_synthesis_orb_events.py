import asyncio
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from backend.schemas import ConfigUpdate
from backend.services import core
from backend.services import synthesis_jobs


def valid_config(root: Path) -> dict[str, object]:
    return {
        "gpt_sovits_root": str(root),
        "text_lang": "zh",
        "prompt_lang": "zh",
        "ref_audio": "reference.wav",
        "ref_text": "参考文本",
        "top_k": 30,
        "top_p": 0.88,
        "temperature": 0.95,
        "speed": 1.0,
        "pause_second": 0.15,
        "sample_steps": 64,
    }


class CallbackPipeline:
    def run(self, request):
        request["semantic_token_callback"](17, 25)
        yield 24000, [0.0]


class SynthesisOrbEventTests(unittest.TestCase):
    @staticmethod
    def _paths() -> set[str]:
        # FastAPI 0.141 keeps included routers as lazy placeholders in ``app.routes``;
        # the OpenAPI document is the stable, fully expanded view.
        app = __import__("backend.app", fromlist=["app"]).app
        return set(app.openapi()["paths"])

    def test_sync_batch_synthesis_endpoint_is_removed_in_favor_of_sse_jobs(self):
        paths = self._paths()
        self.assertNotIn("/api/batch-synthesis", paths)

    def test_pronunciation_patch_import_route_is_available(self):
        paths = self._paths()
        self.assertIn("/api/engine/pronunciation-patch", paths)

    def test_pronunciation_patch_import_checks_then_applies_to_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "GPT-SoVITS"
            text_dir = root / "GPT_SoVITS" / "text"
            text_dir.mkdir(parents=True)
            (text_dir / "chinese.py").write_bytes(b"tone = 1\n")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
            with (text_dir / "chinese.py").open("a", encoding="utf-8", newline="") as output:
                output.write("GP_PINYIN = {}\n")
            patch = subprocess.check_output(
                ["git", "diff", "--", "GPT_SoVITS/text/chinese.py"], cwd=root
            ).decode("utf-8")
            subprocess.run(["git", "restore", "GPT_SoVITS/text/chinese.py"], cwd=root, check=True)

            core.apply_pronunciation_patch(root, "pinyin.patch", io.BytesIO(patch.encode()))
            applied = (text_dir / "chinese.py").read_text(encoding="utf-8")

            self.assertIn("GP_PINYIN", applied)
            with self.assertRaisesRegex(ValueError, "校验失败"):
                core.apply_pronunciation_patch(root, "pinyin.patch", io.BytesIO(patch.encode()))

    def test_pronunciation_patch_import_rejects_unrelated_or_oversized_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "GPT-SoVITS"
            root.mkdir()
            with self.assertRaisesRegex(ValueError, "发音补丁"):
                core.apply_pronunciation_patch(root, "other.patch", io.BytesIO(b"diff --git a/a.py b/a.py\n"))
            with patch.object(core, "MAX_PATCH_BYTES", 2):
                with self.assertRaisesRegex(ValueError, "1 MB"):
                    core.apply_pronunciation_patch(root, "pinyin.patch", io.BytesIO(b"abc"))

    def test_default_config_exposes_v11_orb_colors(self):
        config = core.default_config()
        self.assertEqual(config["orb_synthesis_token_color"], "#75BBFF")
        self.assertEqual(config["orb_settled_color"], "#69DDA4")
        self.assertEqual(config["orb_playback_token_color"], "#F8FBFF")

    def test_config_normalizes_hex_orb_colors_and_rejects_invalid_values(self):
        update = ConfigUpdate(orb_playback_token_color="#ffffff")
        self.assertEqual(update.orb_playback_token_color, "#FFFFFF")
        with self.assertRaises(ValidationError):
            ConfigUpdate(orb_synthesis_token_color="blue")

    def test_custom_orb_colors_round_trip_through_the_persisted_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            config_file = config_dir / "config.json"
            with patch.object(core, "CONFIG_DIR", config_dir), patch.object(core, "CONFIG_FILE", config_file):
                core.save_config({**core.load_config(), "orb_synthesis_token_color": "#123abc", "orb_settled_color": "#456def", "orb_playback_token_color": "#f8fbff"})
                loaded = core.load_config()

        self.assertEqual(loaded["orb_synthesis_token_color"], "#123ABC")
        self.assertEqual(loaded["orb_settled_color"], "#456DEF")
        self.assertEqual(loaded["orb_playback_token_color"], "#F8FBFF")

    def test_config_persistence_has_an_explicit_lock_for_atomic_writes(self):
        self.assertTrue(hasattr(core, "_CONFIG_LOCK"))
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            config_file = config_dir / "config.json"
            with patch.object(core, "CONFIG_DIR", config_dir), patch.object(core, "CONFIG_FILE", config_file):
                core.save_config({**core.load_config(), "orb_synthesis_token_color": "#111111"})
                self.assertTrue(config_file.is_file())
                self.assertFalse((config_dir / "config.json.tmp").exists())

    def test_synth_one_forwards_real_token_counts_to_core_callback(self):
        backend = core.SynthesisBackend()
        backend._pipeline = CallbackPipeline()
        seen: list[tuple[int, int]] = []
        root = Path.cwd()

        backend.synth_one(
            valid_config(root),
            "hello",
            semantic_token_cb=lambda count, hz: seen.append((count, hz)),
        )

        self.assertEqual(seen, [(17, 25)])

    def test_synthesize_lines_emits_tokens_then_completion_for_same_output(self):
        events: list[dict[str, object]] = []

        def fake_synth_one(_cfg, _text, semantic_token_cb=None):
            semantic_token_cb(5, 25)
            return [0.0], 24000

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "line.wav"
            with patch.object(core.SYNTHESIS_BACKEND, "ensure"), patch.object(
                core.SYNTHESIS_BACKEND, "synth_one", side_effect=fake_synth_one
            ), patch.object(core, "save_audio", return_value=output), patch.object(
                core, "append_history"
            ), patch.object(core, "register_download", return_value="download"):
                result = core.synthesize_lines(valid_config(Path(directory)), "one", event_cb=events.append)

        self.assertEqual(events[0], {
            "type": "semantic_token", "output_index": 0, "token_index": 5, "semantic_frame_rate": 25,
        })
        self.assertEqual(
            {key: events[1][key] for key in ("type", "output_index", "semantic_token_count", "semantic_frame_rate")},
            {"type": "output_completed", "output_index": 0, "semantic_token_count": 5, "semantic_frame_rate": 25},
        )
        self.assertEqual(result["outputs"][0]["semantic_token_count"], 5)
        self.assertEqual(result["outputs"][0]["semantic_frame_rate"], 25)

    def test_job_manager_publishes_transient_events_without_adding_them_to_snapshot(self):
        async def publish_and_read_event():
            manager = synthesis_jobs.SynthesisJobManager()
            job = synthesis_jobs._SynthesisJob(
                job_id="job",
                snapshot={"job_id": "job", "status": "running", "phase": "prepare"},
            )
            manager._jobs[job.job_id] = job
            _, queue = manager.subscribe("job")
            manager.update("job", {"phase": "generating"})
            manager.publish("job", {"type": "semantic_token", "output_index": 0, "token_index": 3})
            return manager.snapshot("job"), await queue.get(), await queue.get()

        snapshot, snapshot_event, token_event = asyncio.run(publish_and_read_event())

        self.assertEqual(snapshot["phase"], "generating")
        self.assertEqual(snapshot_event["type"], "snapshot")
        self.assertEqual(token_event["type"], "semantic_token")
        self.assertNotIn("token_index", snapshot)

    def test_job_events_and_snapshots_have_monotonic_revisions(self):
        async def collect_revisions():
            manager = synthesis_jobs.SynthesisJobManager()
            job = synthesis_jobs._SynthesisJob(
                job_id="job",
                snapshot={"job_id": "job", "status": "running", "phase": "prepare"},
            )
            manager._jobs[job.job_id] = job
            _, queue = manager.subscribe("job")
            manager.update("job", {"phase": "generating"})
            manager.publish("job", {"type": "semantic_token", "output_index": 0, "token_index": 1})
            manager.update("job", {"phase": "writing"})
            return [
                (await queue.get())["revision"],
                (await queue.get())["revision"],
                (await queue.get())["revision"],
                manager.snapshot("job")["revision"],
            ]

        self.assertEqual(asyncio.run(collect_revisions()), [1, 2, 3, 3])

    def test_failed_output_emits_failure_event_without_completion(self):
        events: list[dict[str, object]] = []

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(core.SYNTHESIS_BACKEND, "ensure"), patch.object(
                core.SYNTHESIS_BACKEND, "synth_one", side_effect=RuntimeError("bad model")
            ), patch.object(core, "append_history"):
                result = core.synthesize_lines(valid_config(Path(directory)), "one", event_cb=events.append)

        self.assertEqual(events, [{"type": "output_failed", "output_index": 0, "error": "RuntimeError: bad model"}])
        self.assertEqual(result["outputs"][0]["semantic_token_count"], 0)
        self.assertNotIn("output_completed", [event["type"] for event in events])


if __name__ == "__main__":
    unittest.main()
