"""Apply the explicit mixed-language single-pass hooks to a GPT-SoVITS checkout."""

from __future__ import annotations

import argparse
import dataclasses
from pathlib import Path

EXPLICIT_PREPROCESSOR = """    def preprocess_segments(self, segments, version="v2"):
        phones_list, bert_list, norm_text_list = [], [], []
        boundaries, phone_offset = [], 0
        non_empty_segments = [
            (index, segment)
            for index, segment in enumerate(segments)
            if str(segment.get("text", "")).strip()
        ]
        for position, (index, segment) in enumerate(non_empty_segments):
            lang = segment["lang"]
            text = segment["text"].strip()
            is_final = position == len(non_empty_segments) - 1
            pronunciation_text = self.normalize_mixed_pronunciation_text(
                segment.get("pronunciation_text", text).strip(),
                is_final=is_final,
            )
            phones, word2ph, norm_text = self.clean_text_inf(pronunciation_text, lang, version)
            bert = self.get_bert_inf(phones, word2ph, norm_text, lang)
            start = phone_offset
            phones_list.append(phones)
            bert_list.append(bert)
            norm_text_list.append(norm_text)
            phone_offset += len(phones)
            boundaries.append({"index": index, "lang": lang, "start": start, "end": phone_offset})
            if segment.get("pause_after_ms", 0) > 0 and not is_final:
                # Full-width comma maps to one neutral pause phone in all five G2P paths.
                pause_text = "，"
                pause_phones, pause_word2ph, pause_norm = self.clean_text_inf(pause_text, lang, version)
                pause_bert = self.get_bert_inf(pause_phones, pause_word2ph, pause_norm, lang)
                phones_list.append(pause_phones)
                bert_list.append(pause_bert)
                norm_text_list.append(pause_norm)
                phone_offset += len(pause_phones)
        if not phones_list:
            raise ValueError("显式语言片段没有可发音内容。")
        bert = torch.cat(bert_list, dim=1)
        phones = sum(phones_list, [])
        if len(phones) != bert.shape[-1]:
            raise ValueError("显式语言片段的 phones/BERT 长度不一致。")
        return [{"phones": phones, "bert_features": bert, "norm_text": "".join(norm_text_list), "segment_boundaries": boundaries}]

    @staticmethod
    def normalize_mixed_pronunciation_text(text: str, *, is_final: bool) -> str:
        if is_final:
            return text
        normalized = text.rstrip("。！？!?；;：:….").rstrip()
        return normalized or text

"""

TTS_SEGMENT_BRANCH = """            if text_lang == "segment_mixed":
                if not isinstance(text_segments, list) or not text_segments:
                    raise ValueError("segment_mixed 需要非空 text_segments。")
                data = self.text_preprocessor.preprocess_segments(text_segments, self.configs.version)
            else:
                data = self.text_preprocessor.preprocess(text, text_lang, text_split_method, self.configs.version)
"""

TTS_STREAM_GUARD = """        if text_lang == "segment_mixed" and (return_fragment or streaming_mode):
            raise ValueError("segment_mixed 不支持流式或分段返回。")
"""

TTS_LEGACY_SEGMENT_BRANCH = """        if not (return_fragment or streaming_mode):
            if text_lang == "segment_mixed":
                if return_fragment or streaming_mode:
                    raise ValueError("segment_mixed 不支持流式或分段返回。")
                if not isinstance(text_segments, list) or not text_segments:
                    raise ValueError("segment_mixed 需要非空 text_segments。")
                data = self.text_preprocessor.preprocess_segments(text_segments, self.configs.version)
            else:
                data = self.text_preprocessor.preprocess(text, text_lang, text_split_method, self.configs.version)
"""

TTS_MIXED_EOS_FLOOR = """                mixed_eos_floor = inputs.get("mixed_eos_floor")
                mixed_token_cap = inputs.get("mixed_token_cap")
                if text_lang == "segment_mixed":
                    phone_count = max(1, int(batch_phones_len[0].item()))
                    min_token_num = (
                        max(0, int(mixed_eos_floor))
                        if mixed_eos_floor is not None
                        else max(11, int(math.ceil(phone_count * 1.5)))
                    )
                    max_token_num = max(
                        min_token_num,
                        max(11, int(math.ceil(phone_count * 2.5)))
                        if mixed_token_cap is None
                        else max(0, int(mixed_token_cap)),
                    )
                else:
                    min_token_num = 11
                    max_token_num = -1
"""

T2S_MIN_TOKEN_FLOOR = """        min_token_num = max(0, int(kwargs.get("min_token_num", 11)))
        max_token_num = int(kwargs.get("max_token_num", -1))
"""

T2S_MIXED_TOKEN_CAP = """        for idx in tqdm(range(1500)):
            generated_token_num = y.shape[1] - prefix_len
            if max_token_num >= 0 and generated_token_num >= max_token_num:
                stop = True
                idx = max(0, generated_token_num - 1)
                if y.shape[1] == 0:
                    y = torch.zeros(x.shape[0], 1, dtype=torch.long, device=x.device)
                if streaming_mode:
                    yield y[:, curr_ptr:] if curr_ptr < y.shape[1] else None, True
                break
"""

@dataclasses.dataclass
class PatchReport:
    root: Path
    changed: bool = False
    messages: list[str] = dataclasses.field(default_factory=list)


def _engine_root(root: Path) -> Path:
    root = root.expanduser().resolve()
    candidate = root / "GPT_SoVITS"
    if candidate.is_dir():
        return candidate
    if root.name == "GPT_SoVITS":
        return root
    raise FileNotFoundError(f"未找到 GPT-SoVITS 引擎目录：{root}")


def _read_source(path: Path) -> tuple[str, str]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise FileNotFoundError(f"未找到需要打补丁的文件：{path}") from error
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError(f"打补丁失败：无法按 UTF-8 读取 {path}") from error
    newline = "\r\n" if "\r\n" in source else "\n"
    return source.replace("\r\n", "\n"), newline


def _write_source(path: Path, source: str, newline: str) -> None:
    path.write_bytes(source.replace("\n", newline).encode("utf-8"))


def _replace_once(path: Path, source: str, old: str, new: str, label: str, report: PatchReport) -> str:
    if new in source:
        return source
    if source.count(old) != 1:
        raise RuntimeError(f"打补丁失败：{path}: {label} 锚点数量不是 1。")
    report.changed = True
    report.messages.append(label)
    return source.replace(old, new, 1)


def apply_mixed_language_patches(root: str | Path) -> PatchReport:
    """Inject explicit Segment IR compilation without changing model weights."""
    engine_root = _engine_root(Path(root))
    preprocessor_path = engine_root / "TTS_infer_pack" / "TextPreprocessor.py"
    tts_path = engine_root / "TTS_infer_pack" / "TTS.py"
    t2s_path = engine_root / "AR" / "models" / "t2s_model.py"
    report = PatchReport(root=engine_root)

    preprocessor, preprocessor_newline = _read_source(preprocessor_path)
    tts, tts_newline = _read_source(tts_path)
    t2s, t2s_newline = _read_source(t2s_path)

    preprocessor_anchor = "    def get_bert_feature(self, text: str, word2ph: list) -> torch.Tensor:\n"
    preprocessor = _replace_once(
        preprocessor_path,
        preprocessor,
        preprocessor_anchor,
        EXPLICIT_PREPROCESSOR + preprocessor_anchor,
        "注入显式语言片段预处理",
        report,
    )

    tts = _replace_once(
        tts_path,
        tts,
        '    v1_languages: list = ["auto", "en", "zh", "ja", "all_zh", "all_ja"]\n',
        '    v1_languages: list = ["auto", "en", "zh", "ja", "all_zh", "all_ja", "segment_mixed"]\n',
        "注册 segment_mixed 语言模式",
        report,
    )
    tts = _replace_once(
        tts_path,
        tts,
        '    v2_languages: list = ["auto", "auto_yue", "en", "zh", "ja", "yue", "ko", "all_zh", "all_ja", "all_yue", "all_ko"]\n',
        '    v2_languages: list = ["auto", "auto_yue", "en", "zh", "ja", "yue", "ko", "all_zh", "all_ja", "all_yue", "all_ko", "segment_mixed"]\n',
        "注册 segment_mixed 语言模式",
        report,
    )
    tts = _replace_once(
        tts_path,
        tts,
        '        text_lang: str = inputs.get("text_lang", "")\n',
        '        text_lang: str = inputs.get("text_lang", "")\n        text_segments = inputs.get("text_segments")\n',
        "读取显式语言片段",
        report,
    )
    tts = _replace_once(
        tts_path,
        tts,
        '        streaming_mode = inputs.get("streaming_mode", False)\n',
        '        streaming_mode = inputs.get("streaming_mode", False)\n' + TTS_STREAM_GUARD,
        "拒绝显式语言流式推理",
        report,
    )
    normal_branch = '        if not (return_fragment or streaming_mode):\n' + TTS_SEGMENT_BRANCH
    if normal_branch not in tts:
        if '        if not (return_fragment or streaming_mode):\n            data = self.text_preprocessor.preprocess(text, text_lang, text_split_method, self.configs.version)\n' in tts:
            tts = _replace_once(
                tts_path,
                tts,
                '        if not (return_fragment or streaming_mode):\n            data = self.text_preprocessor.preprocess(text, text_lang, text_split_method, self.configs.version)\n',
                normal_branch,
                "接入单次显式语言推理",
                report,
            )
        elif TTS_LEGACY_SEGMENT_BRANCH in tts:
            tts = _replace_once(
                tts_path,
                tts,
                TTS_LEGACY_SEGMENT_BRANCH,
                normal_branch,
                "修正单次显式语言推理分支",
                report,
            )
        else:
            raise RuntimeError(f"打补丁失败：{tts_path}: 接入单次显式语言推理 锚点数量不是 1。")

    tts = _replace_once(
        tts_path,
        tts,
        '                max_len = item["max_len"]\n',
        '                max_len = item["max_len"]\n' + TTS_MIXED_EOS_FLOOR,
        "按混合片段 phone 数设置 EOS 下限",
        report,
    )
    tts = _replace_once(
        tts_path,
        tts,
        "                        repetition_penalty=repetition_penalty,\n                        semantic_token_callback=report_semantic_tokens,\n",
        "                        repetition_penalty=repetition_penalty,\n                        min_token_num=min_token_num,\n                        max_token_num=max_token_num,\n                        semantic_token_callback=report_semantic_tokens,\n",
        "传递混合请求 EOS 上下限",
        report,
    )

    t2s = _replace_once(
        t2s_path,
        t2s,
        "        check_token_num = 2\n",
        "        check_token_num = 2\n" + T2S_MIN_TOKEN_FLOOR,
        "接收混合请求 EOS 下限",
        report,
    )
    t2s = _replace_once(
        t2s_path,
        t2s,
        "            if idx == 0:\n                xy_attn_mask = None\n            if idx < 11:  ###至少预测出10个token不然不给停止（0.4s）\n",
        "            if idx == 0:\n                xy_attn_mask = None\n            if idx < min_token_num - 1:  ###混合请求按 phone 数抑制过早 EOS\n",
        "应用混合请求 EOS 下限",
        report,
    )
    cap_anchor = "        for idx in tqdm(range(1500)):\n            token_counter+=1\n"
    cap_replacement = T2S_MIXED_TOKEN_CAP + "            token_counter+=1\n"
    if cap_anchor not in t2s:
        cap_anchor = "        for idx in range(1500):\n            token_counter+=1\n"
        cap_replacement = cap_replacement.replace("tqdm(range(1500))", "range(1500)")
    if "generated_token_num = y.shape[1] - prefix_len" not in t2s:
        t2s = _replace_once(
            t2s_path,
            t2s,
            cap_anchor,
            cap_replacement,
            "应用混合请求 token 上限",
            report,
        )

    preprocessor_original, _ = _read_source(preprocessor_path)
    tts_original, _ = _read_source(tts_path)
    t2s_original, _ = _read_source(t2s_path)
    if preprocessor != preprocessor_original:
        _write_source(preprocessor_path, preprocessor, preprocessor_newline)
    if tts != tts_original:
        _write_source(tts_path, tts, tts_newline)
    if t2s != t2s_original:
        _write_source(t2s_path, t2s, t2s_newline)
    if not report.changed:
        report.messages.append("混合语言单次推理补丁已存在")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", help="GPT-SoVITS 根目录，或其内部 GPT_SoVITS 目录")
    args = parser.parse_args()
    try:
        report = apply_mixed_language_patches(args.root)
    except (FileNotFoundError, RuntimeError) as error:
        print(error)
        return 1
    for message in report.messages:
        print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
