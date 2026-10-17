from pathlib import Path

import pytest

from tools.gpt_sovits_mixed_language_patch import apply_mixed_language_patches


TEXT_PREPROCESSOR = '''\
import torch

class TextPreprocessor:
    def clean_text_inf(self, text, language, version="v2"):
        return [], [], ""

    def get_bert_inf(self, phones, word2ph, norm_text, language):
        return torch.zeros((1024, len(phones)))

    def get_bert_feature(self, text: str, word2ph: list) -> torch.Tensor:
        return torch.zeros((1024, len(word2ph)))
'''

TTS = '''\
class TTS_Config:
    v1_languages: list = ["auto", "en", "zh", "ja", "all_zh", "all_ja"]
    v2_languages: list = ["auto", "auto_yue", "en", "zh", "ja", "yue", "ko", "all_zh", "all_ja", "all_yue", "all_ko"]

class TTS:
    def run(self, inputs: dict):
        text: str = inputs.get("text", "")
        text_lang: str = inputs.get("text_lang", "")
        text_split_method: str = inputs.get("text_split_method", "cut1")
        return_fragment = inputs.get("return_fragment", False)
        streaming_mode = inputs.get("streaming_mode", False)
        data: list = None
        if not (return_fragment or streaming_mode):
            data = self.text_preprocessor.preprocess(text, text_lang, text_split_method, self.configs.version)
                    batch_phones_len = [1]
                    all_phoneme_lens = [1]
                    max_len = item["max_len"]
                    pred_semantic_list, idx_list = self.t2s_model.model.infer_panel(
                        repetition_penalty=repetition_penalty,
                        semantic_token_callback=report_semantic_tokens,
                    )
'''

T2S_MODEL = '''\
class Text2SemanticDecoder:
    def infer_panel_naive(self, **kwargs):
        check_token_num = 2
        for idx in range(1500):
            token_counter+=1
            if idx == 0:
                xy_attn_mask = None
            if idx < 11:  ###至少预测出10个token不然不给停止（0.4s）
                pass
'''


def make_engine_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "GPT-SoVITS"
    engine = root / "GPT_SoVITS"
    preprocessor = engine / "TTS_infer_pack" / "TextPreprocessor.py"
    tts = engine / "TTS_infer_pack" / "TTS.py"
    t2s_model = engine / "AR" / "models" / "t2s_model.py"
    preprocessor.parent.mkdir(parents=True)
    t2s_model.parent.mkdir(parents=True)
    preprocessor.write_text(TEXT_PREPROCESSOR, encoding="utf-8")
    tts.write_text(TTS, encoding="utf-8")
    t2s_model.write_text(T2S_MODEL, encoding="utf-8")
    return root


def test_patch_injects_explicit_segment_preprocessor_idempotently(tmp_path):
    root = make_engine_fixture(tmp_path)

    first = apply_mixed_language_patches(root)
    second = apply_mixed_language_patches(root)
    source = (root / "GPT_SoVITS/TTS_infer_pack/TextPreprocessor.py").read_text(encoding="utf-8")

    assert first.changed is True
    assert second.changed is False
    assert 'def preprocess_segments(self, segments, version="v2")' in source
    assert 'segment["lang"]' in source
    assert 'segment.get("pronunciation_text", text)' in source
    assert "LangSegmenter.getTexts" not in source.split("def preprocess_segments", 1)[1].split(
        "def get_bert_feature", 1
    )[0]


def test_patch_normalizes_nonfinal_terminal_punctuation_inside_pronunciation_path(tmp_path):
    root = make_engine_fixture(tmp_path)

    apply_mixed_language_patches(root)
    source = (root / "GPT_SoVITS/TTS_infer_pack/TextPreprocessor.py").read_text(encoding="utf-8")

    assert "normalize_mixed_pronunciation_text" in source
    assert "is_final" in source
    assert "non_empty_segments" in source
    assert 'str(segment.get("text", "")).strip()' in source


def test_patch_uses_language_neutral_single_phone_boundary_marker(tmp_path):
    root = make_engine_fixture(tmp_path)

    apply_mixed_language_patches(root)
    source = (root / "GPT_SoVITS/TTS_infer_pack/TextPreprocessor.py").read_text(encoding="utf-8")

    assert 'pause_text = "，"' in source
    assert 'clean_text_inf(pause_text, lang, version)' in source
    assert 'clean_text_inf(",", lang, version)' not in source


def test_patch_preserves_utf8_bom_and_windows_newlines(tmp_path):
    root = make_engine_fixture(tmp_path)
    for relative in (
        "GPT_SoVITS/TTS_infer_pack/TextPreprocessor.py",
        "GPT_SoVITS/TTS_infer_pack/TTS.py",
    ):
        path = root / relative
        path.write_bytes(b"\xef\xbb\xbf" + path.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8"))

    apply_mixed_language_patches(root)

    source = (root / "GPT_SoVITS/TTS_infer_pack/TextPreprocessor.py").read_bytes()
    assert source.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in source


def test_patch_rejects_missing_engine_files(tmp_path):
    with pytest.raises(FileNotFoundError, match="GPT-SoVITS"):
        apply_mixed_language_patches(tmp_path / "GPT-SoVITS")


def test_patch_rejects_changed_upstream_anchors(tmp_path):
    root = make_engine_fixture(tmp_path)
    path = root / "GPT_SoVITS/TTS_infer_pack/TextPreprocessor.py"
    path.write_text(path.read_text(encoding="utf-8").replace("def get_bert_feature", "def changed"), encoding="utf-8")

    with pytest.raises(RuntimeError, match="TextPreprocessor.py"):
        apply_mixed_language_patches(root)


def test_patch_injects_tts_contract(tmp_path):
    root = make_engine_fixture(tmp_path)

    apply_mixed_language_patches(root)
    source = (root / "GPT_SoVITS/TTS_infer_pack/TTS.py").read_text(encoding="utf-8")

    assert '"segment_mixed"' in source
    assert 'text_segments = inputs.get("text_segments")' in source
    assert "preprocess_segments(text_segments" in source
    assert "segment_mixed" in source and "不支持流式" in source


def test_patch_adds_phone_scaled_eos_floor_for_mixed_requests(tmp_path):
    root = make_engine_fixture(tmp_path)

    apply_mixed_language_patches(root)
    tts_source = (root / "GPT_SoVITS/TTS_infer_pack/TTS.py").read_text(encoding="utf-8")
    t2s_source = (root / "GPT_SoVITS/AR/models/t2s_model.py").read_text(encoding="utf-8")

    assert "min_token_num" in tts_source
    assert "segment_mixed" in tts_source and "all_phoneme_lens" in tts_source
    assert "batch_phones_len[0].item()" in tts_source
    assert "phone_count * 1.5" in tts_source
    assert "phone_count * 2.5" in tts_source
    assert "min_token_num - 1" in t2s_source


def test_patch_allows_explicit_mixed_eos_floor_override(tmp_path):
    root = make_engine_fixture(tmp_path)

    apply_mixed_language_patches(root)
    tts_source = (root / "GPT_SoVITS/TTS_infer_pack/TTS.py").read_text(encoding="utf-8")

    assert 'mixed_eos_floor = inputs.get("mixed_eos_floor")' in tts_source
    assert "mixed_eos_floor is not None" in tts_source


def test_patch_adds_mixed_upper_token_cap_to_prevent_tail_repeat(tmp_path):
    root = make_engine_fixture(tmp_path)

    apply_mixed_language_patches(root)
    tts_source = (root / "GPT_SoVITS/TTS_infer_pack/TTS.py").read_text(encoding="utf-8")
    t2s_source = (root / "GPT_SoVITS/AR/models/t2s_model.py").read_text(encoding="utf-8")

    assert "mixed_token_cap" in tts_source
    assert "max_token_num" in tts_source
    assert "generated_token_num >= max_token_num" in t2s_source


def test_installers_apply_mixed_language_patch():
    repo_root = Path(__file__).resolve().parents[2]
    powershell = (repo_root / "setup.ps1").read_text(encoding="utf-8")
    shell = (repo_root / "setup.sh").read_text(encoding="utf-8")
    assert "gpt_sovits_mixed_language_patch.py" in powershell
    assert "gpt_sovits_mixed_language_patch.py" in shell