"""Apply the idempotent semantic-token callback hooks to a local GPT-SoVITS checkout."""

from __future__ import annotations

import argparse
import dataclasses
from pathlib import Path


T2S_CALLBACK_READ = 'semantic_token_callback = kwargs.get("semantic_token_callback")'
T2S_TOKEN_EMIT = "semantic_token_callback(y.shape[1] - prefix_len)"
T2S_NAIVE_EMIT = """            if idx == 1499:
                stop = True

            if callable(semantic_token_callback):
                try:
                    semantic_token_callback(y.shape[1] - prefix_len)
                except Exception:
                    pass

            if stop:
"""
T2S_NAIVE_ANCHOR = """            if idx == 1499:
                stop = True

            if stop:
"""
TTS_WRAPPER_READ = 'semantic_token_callback = inputs.get("semantic_token_callback")'

T2S_CHUNK_ANCHOR = 'chunk_split_thershold = kwargs.get("chunk_split_thershold", 0.3)'
T2S_OUTPUT_ANCHOR = "y = torch.concat([y, samples], dim=1)"
TTS_INPUT_ANCHOR = 'fixed_length_chunk = inputs.get("fixed_length_chunk", False)'

T2S_EMIT_BLOCK = """

            if callable(semantic_token_callback):
                try:
                    semantic_token_callback(y.shape[1] - prefix_len)
                except Exception:
                    pass
"""

TTS_WRAPPER_BLOCK = '''
        semantic_token_callback = inputs.get("semantic_token_callback")

        try:
            semantic_frame_rate = int(str(self.configs.semantic_frame_rate).removesuffix("hz"))
        except (AttributeError, TypeError, ValueError):
            semantic_frame_rate = int(self.configs.hz)

        def report_semantic_tokens(count: int) -> None:
            if not callable(semantic_token_callback):
                return
            try:
                semantic_token_callback(int(count), semantic_frame_rate)
            except Exception:
                pass
'''

TTS_CALL_SITES = (
    (
        """                        early_stop_num=self.configs.hz * self.configs.max_sec,
                        max_len=max_len,
                        repetition_penalty=repetition_penalty,
                    )""",
        """                        early_stop_num=self.configs.hz * self.configs.max_sec,
                        max_len=max_len,
                        repetition_penalty=repetition_penalty,
                        semantic_token_callback=report_semantic_tokens,
                    )""",
    ),
    (
        """                        mute_emb_sim_matrix=self.configs.mute_emb_sim_matrix if not fixed_length_chunk else None,
                        chunk_split_thershold=chunk_split_thershold,
                    )""",
        """                        mute_emb_sim_matrix=self.configs.mute_emb_sim_matrix if not fixed_length_chunk else None,
                        chunk_split_thershold=chunk_split_thershold,
                        semantic_token_callback=report_semantic_tokens,
                    )""",
    ),
)


@dataclasses.dataclass
class PatchReport:
    root: Path
    changed: bool = False
    messages: list[str] = dataclasses.field(default_factory=list)


def _engine_root(root: Path) -> Path:
    candidate = root / "GPT_SoVITS"
    if candidate.is_dir():
        return candidate
    if root.name == "GPT_SoVITS":
        return root
    raise FileNotFoundError(f"未找到 GPT-SoVITS 引擎目录：{root}")


def _replace_once(text: str, old: str, new: str, label: str, report: PatchReport) -> str:
    if new in text:
        return text
    if old not in text:
        raise RuntimeError(f"打补丁失败：找不到 {label} 锚点。")
    report.changed = True
    report.messages.append(label)
    return text.replace(old, new, 1)


def apply_semantic_token_patches(root: str | Path) -> PatchReport:
    """Install callbacks without modifying any other upstream behavior."""
    engine_root = _engine_root(Path(root).expanduser().resolve())
    report = PatchReport(root=engine_root)
    t2s_path = engine_root / "AR" / "models" / "t2s_model.py"
    tts_path = engine_root / "TTS_infer_pack" / "TTS.py"
    for path in (t2s_path, tts_path):
        if not path.is_file():
            raise FileNotFoundError(f"未找到需要打补丁的文件：{path}")

    t2s_raw = t2s_path.read_bytes().decode("utf-8")
    t2s_newline = "\r\n" if "\r\n" in t2s_raw else "\n"
    t2s = t2s_raw.replace("\r\n", "\n")
    original_t2s = t2s
    if T2S_CALLBACK_READ not in t2s:
        t2s = _replace_once(
            t2s,
            T2S_CHUNK_ANCHOR,
            f'{T2S_CHUNK_ANCHOR}\n        {T2S_CALLBACK_READ}',
            "读取 AR 语义 token 回调",
            report,
        )
    if T2S_TOKEN_EMIT not in t2s:
        t2s = _replace_once(
            t2s,
            T2S_OUTPUT_ANCHOR,
            f"{T2S_OUTPUT_ANCHOR}{T2S_EMIT_BLOCK}",
            "逐步上报 AR 语义 token",
            report,
        )
    if T2S_NAIVE_EMIT not in t2s:
        t2s = _replace_once(
            t2s,
            T2S_NAIVE_ANCHOR,
            T2S_NAIVE_EMIT,
            "逐步上报非并行 AR 语义 token",
            report,
        )
    if t2s != original_t2s:
        t2s_path.write_text(t2s.replace("\n", t2s_newline), encoding="utf-8", newline="")

    tts_raw = tts_path.read_bytes().decode("utf-8")
    tts_newline = "\r\n" if "\r\n" in tts_raw else "\n"
    tts = tts_raw.replace("\r\n", "\n")
    original_tts = tts
    legacy_hz_callback = "semantic_token_callback(int(count), int(self.configs.hz))"
    modern_hz_callback = "semantic_token_callback(int(count), semantic_frame_rate)"
    if TTS_WRAPPER_READ in tts and legacy_hz_callback in tts:
        tts = _replace_once(tts, """        semantic_token_callback = inputs.get("semantic_token_callback")
""", '''        semantic_token_callback = inputs.get("semantic_token_callback")

        try:
            semantic_frame_rate = int(str(self.configs.semantic_frame_rate).removesuffix("hz"))
        except (AttributeError, TypeError, ValueError):
            semantic_frame_rate = int(self.configs.hz)

''', "解析真实语义帧率", report)
        tts = _replace_once(tts, legacy_hz_callback, modern_hz_callback, "上报真实语义帧率", report)
    if TTS_WRAPPER_READ not in tts:
        tts = _replace_once(
            tts,
            TTS_INPUT_ANCHOR,
            f"{TTS_INPUT_ANCHOR}{TTS_WRAPPER_BLOCK}",
            "创建 TTS 语义 token 包装回调",
            report,
        )
    for old, new in TTS_CALL_SITES:
        tts = _replace_once(tts, old, new, "接入推理调用 token 回调", report)
    if tts != original_tts:
        tts_path.write_text(tts.replace("\n", tts_newline), encoding="utf-8", newline="")

    if not report.changed:
        report.messages.append("语义 token 回调补丁已存在")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", help="GPT-SoVITS 根目录，或其内部 GPT_SoVITS 目录")
    args = parser.parse_args()
    try:
        report = apply_semantic_token_patches(args.root)
    except (FileNotFoundError, RuntimeError) as error:
        print(error)
        return 1
    for message in report.messages:
        print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
