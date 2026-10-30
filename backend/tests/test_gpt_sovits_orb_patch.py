import tempfile
import unittest
from pathlib import Path

from tools.gpt_sovits_orb_patch import apply_semantic_token_patches

T2S_SOURCE = '''        chunk_split_thershold = kwargs.get("chunk_split_thershold", 0.3)
        check_token_num = 2


            y = torch.concat([y, samples], dim=1)


            if idx == 1499:
                stop = True

            if stop:


'''

T2S_NONPARALLEL_SOURCE = '''        semantic_token_callback = kwargs.get("semantic_token_callback")

            y = torch.concat([y, samples], dim=1)

            if callable(semantic_token_callback):
                semantic_token_callback(y.shape[1] - prefix_len)

        semantic_token_callback = kwargs.get("semantic_token_callback")

            if idx == 1499:
                stop = True

            if stop:
'''

TTS_SOURCE = '''        self.configs.semantic_frame_rate = "25hz"
        fixed_length_chunk = inputs.get("fixed_length_chunk", False)
                        early_stop_num=self.configs.hz * self.configs.max_sec,
                        max_len=max_len,
                        repetition_penalty=repetition_penalty,
                    )
                        mute_emb_sim_matrix=self.configs.mute_emb_sim_matrix if not fixed_length_chunk else None,
                        chunk_split_thershold=chunk_split_thershold,
                    )
'''


class GptSovitsOrbPatchTests(unittest.TestCase):
    def test_setup_scripts_use_the_prepared_python_interpreter(self):
        repo_root = Path(__file__).resolve().parents[2]
        powershell = (repo_root / "setup.ps1").read_text(encoding="utf-8")
        shell = (repo_root / "setup.sh").read_text(encoding="utf-8")

        self.assertIn('& $PythonExe (Join-Path $RepoRoot "tools\\gpt_sovits_orb_patch.py")', powershell)
        self.assertNotIn('python (Join-Path $RepoRoot "tools\\gpt_sovits_orb_patch.py")', powershell)
        self.assertIn('"$PYBIN" "$REPO_ROOT/tools/gpt_sovits_orb_patch.py" "$GPT_ROOT"', shell)
        self.assertNotIn('python3 "$REPO_ROOT/tools/gpt_sovits_orb_patch.py"', shell)

    def test_installs_real_model_callbacks_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "GPT-SoVITS"
            t2s_path = root / "GPT_SoVITS" / "AR" / "models" / "t2s_model.py"
            tts_path = root / "GPT_SoVITS" / "TTS_infer_pack" / "TTS.py"
            t2s_path.parent.mkdir(parents=True)
            tts_path.parent.mkdir(parents=True)
            t2s_path.write_text(T2S_SOURCE, encoding="utf-8")
            tts_path.write_text(TTS_SOURCE, encoding="utf-8")

            report = apply_semantic_token_patches(root)
            unchanged = apply_semantic_token_patches(root)

            source = t2s_path.read_text(encoding="utf-8")
            wrapper = tts_path.read_text(encoding="utf-8")
            self.assertTrue(report.changed)
            self.assertFalse(unchanged.changed)
            self.assertIn('semantic_token_callback = kwargs.get("semantic_token_callback")', source)
            self.assertIn("semantic_token_callback(y.shape[1] - prefix_len)", source)
            self.assertIn('semantic_token_callback = inputs.get("semantic_token_callback")', wrapper)
            self.assertIn('semantic_frame_rate = int(str(self.configs.semantic_frame_rate).removesuffix("hz"))', wrapper)
            self.assertIn("semantic_token_callback(int(count), semantic_frame_rate)", wrapper)
            self.assertEqual(wrapper.count("semantic_token_callback=report_semantic_tokens"), 2)

    def test_patches_nonparallel_inference_semantic_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "GPT-SoVITS"
            t2s_path = root / "GPT_SoVITS" / "AR" / "models" / "t2s_model.py"
            tts_path = root / "GPT_SoVITS" / "TTS_infer_pack" / "TTS.py"
            t2s_path.parent.mkdir(parents=True)
            tts_path.parent.mkdir(parents=True)
            t2s_path.write_text(T2S_NONPARALLEL_SOURCE, encoding="utf-8")
            tts_path.write_text(TTS_SOURCE, encoding="utf-8")

            apply_semantic_token_patches(root)

            source = t2s_path.read_text(encoding="utf-8")
            self.assertEqual(
                source.count("semantic_token_callback(y.shape[1] - prefix_len)"),
                2,
            )
            self.assertIn("if idx == 1499:\n                stop = True", source)
            self.assertIn(
                "if idx == 1499:\n"
                "                stop = True\n"
                "\n"
                "            if callable(semantic_token_callback):",
                source,
            )
            self.assertIn(
                "except Exception:\n"
                "                    pass\n"
                "\n"
                "            if stop:",
                source,
            )
            self.assertNotIn("if stop:\n\n            if callable(semantic_token_callback):", source)

    def test_supports_windows_line_endings_from_a_fresh_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "GPT-SoVITS"
            t2s_path = root / "GPT_SoVITS" / "AR" / "models" / "t2s_model.py"
            tts_path = root / "GPT_SoVITS" / "TTS_infer_pack" / "TTS.py"
            t2s_path.parent.mkdir(parents=True)
            tts_path.parent.mkdir(parents=True)
            t2s_source = T2S_SOURCE.replace("\r\n", "\n").replace("\n", "\r\n")
            tts_source = TTS_SOURCE.replace("\r\n", "\n").replace("\n", "\r\n")
            t2s_path.write_bytes(t2s_source.encode("utf-8"))
            tts_path.write_bytes(tts_source.encode("utf-8"))

            report = apply_semantic_token_patches(root)

            self.assertTrue(report.changed)
            self.assertIn("semantic_token_callback(y.shape[1] - prefix_len)", t2s_path.read_text(encoding="utf-8"))
            applied_tts = tts_path.read_text(encoding="utf-8")
            self.assertEqual(applied_tts.count("semantic_token_callback=report_semantic_tokens"), 2)
            self.assertGreater(t2s_path.read_bytes().count(b"\r\n"), 0)
            self.assertGreater(tts_path.read_bytes().count(b"\r\n"), 0)


if __name__ == "__main__":
    unittest.main()
