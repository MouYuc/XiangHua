from itertools import combinations

import numpy as np
import pytest

from backend.services import core

SUPPORTED_LANGUAGES = ("zh", "yue", "en", "ja", "ko")
SAMPLE_TEXT = {
    "zh": "你好",
    "yue": "早晨",
    "en": "hello",
    "ja": "こんにちは",
    "ko": "안녕하세요",
}


@pytest.mark.parametrize("size", range(2, len(SUPPORTED_LANGUAGES) + 1))
def test_every_multilingual_combination_is_one_inference_pass(monkeypatch, size):
    calls = []

    def fake_synth_one(cfg, text, semantic_token_cb=None, text_segments=None):
        calls.append((cfg["text_lang"], text, text_segments))
        return np.zeros(72000, dtype=np.float32), 24000

    monkeypatch.setattr(core.SYNTHESIS_BACKEND, "synth_one", fake_synth_one)
    for languages in combinations(SUPPORTED_LANGUAGES, size):
        calls.clear()
        segments = [(language, SAMPLE_TEXT[language]) for language in languages]
        audio, sample_rate = core.SYNTHESIS_BACKEND.synth_segments({"text_lang": "zh"}, segments)

        assert len(calls) == 1, languages
        assert calls[0][0] == "segment_mixed"
        assert [item["lang"] for item in calls[0][2]] == list(languages)
        assert len(audio) == 72000, languages
        assert sample_rate == 24000


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("<en>你好</en>", "英语"),
        ("<ja>hello</ja>", "日语"),
        ("<ko>hello</ko>", "韩语"),
    ],
)
def test_language_validation_rejects_script_mismatch(text, message):
    with pytest.raises(ValueError, match=message):
        core.validate_synthesis_language_text(text, {"text_lang": "zh"})


def test_language_validation_accepts_digit_only_english():
    core.validate_synthesis_language_text("<en>2026</en>", {"text_lang": "zh"})


@pytest.mark.parametrize(
    "text",
    [
        "</en>Hello",
        "<en>Hello</ja>",
        "<en><ja>Hello</ja></en>",
        "<en></en>",
        "<en>   </en>",
        "<en>Hello",
    ],
)
def test_language_parser_rejects_malformed_or_empty_markers(text):
    with pytest.raises(ValueError):
        core.parse_language_segments(text)


@pytest.mark.parametrize("text", ["<fr>bonjour</fr>", "<de>Hallo</de>", "<es>hola</es>"])
def test_language_parser_rejects_unsupported_language_tags(text):
    with pytest.raises(ValueError, match="不支持"):
        core.parse_language_segments(text)


def test_trim_segment_edges_handles_clip_shorter_than_one_frame():
    audio = np.zeros(80, dtype=np.float32)
    audio[30:50] = 0.2

    trimmed = core.trim_segment_edges(audio, 24000)

    assert trimmed.size > 0


def test_seeded_synthesis_rejects_zero_sample_rate(monkeypatch):
    def fake_synth_one(cfg, text, semantic_token_cb=None):
        return np.zeros(10, dtype=np.float32), 0

    monkeypatch.setattr(core.SYNTHESIS_BACKEND, "synth_one", fake_synth_one)
    with pytest.raises(ValueError, match="采样率"):
        core.SYNTHESIS_BACKEND._synthesize_seeded({"speed": 1.0}, "zh", "你好")


def test_language_validation_ignores_comment_only_lines_and_empty_input():
    core.validate_synthesis_language_text("\n# <en>你好</en>\n", {"text_lang": "zh"})
    core.validate_synthesis_language_text("   ", {"text_lang": "zh"})


def test_composed_mixed_text_strips_fragment_whitespace_and_preserves_order():
    assert core.compose_mixed_language_text(
        [("zh", "  你好  "), ("en", "  Project  "), ("ja", "こんにちは")]
    ) == "你好,Project,こんにちは"
    assert core.compose_mixed_language_text(
        [("zh", "  你好  "), ("ja", " こんにちは "), ("ko", "안녕 ")]
    ) == "你好,こんにちは,안녕"


def test_composed_mixed_text_inserts_pauses_at_language_boundaries():
    assert core.compose_mixed_language_text(
        [("zh", "你好"), ("en", "Hello"), ("ja", "こんにちは"), ("ko", "안녕하세요")]
    ) == "你好,Hello,こんにちは,안녕하세요"
    assert core.compose_mixed_language_text(
        [("en", "Hello"), ("zh", "，欢迎")]
    ) == "Hello,欢迎"


def test_mixed_render_applies_accent_only_to_chinese():
    assert core.render_language_segments(
        [("zh", "不是"), ("en", "Hello")],
        {"auto_lightmid": True, "text_lang": "zh"},
    ) == [("zh", "唔系"), ("en", "Hello")]


def test_english_pronunciation_hint_spells_initialisms_without_expanding_surface_text():
    assert core.normalize_english_pronunciations("AI Voice Lab") == "A I, Voice Lab"
    assert core.normalize_english_pronunciations("A.I. Voice Lab") == "A I, Voice Lab"
    assert core.normalize_english_pronunciations("A I Voice Lab") == "A I, Voice Lab"
    assert core.normalize_english_pronunciations("API GPT-4") == "A P I, G P T 4"
    assert core.normalize_english_pronunciations("NASA NATO") == "nasa, nato"


def test_chinese_cantonese_multilingual_route_uses_auto(monkeypatch):
    calls = []

    def fake_synth_one(cfg, text, semantic_token_cb=None, text_segments=None):
        calls.append((cfg["text_lang"], text, text_segments))
        return np.zeros(72000, dtype=np.float32), 24000

    monkeypatch.setattr(core.SYNTHESIS_BACKEND, "synth_one", fake_synth_one)
    core.SYNTHESIS_BACKEND.synth_segments(
        {"text_lang": "zh"},
        [("zh", "我记得"), ("yue", "佢今日喺屋企"), ("en", "at home")],
    )

    assert len(calls) == 1
    assert calls[0][0] == "segment_mixed"
    assert [item["lang"] for item in calls[0][2]] == ["zh", "yue", "en"]


def test_single_english_route_uses_hint_but_keeps_surface_text(monkeypatch):
    calls = []

    def fake_synth_one(cfg, text, semantic_token_cb=None):
        calls.append((cfg["text_lang"], text))
        return np.zeros(24000, dtype=np.float32), 24000

    monkeypatch.setattr(core.SYNTHESIS_BACKEND, "synth_one", fake_synth_one)
    core.SYNTHESIS_BACKEND.synth_segments({"text_lang": "en", "speed": 1.0}, [("en", "AI")])

    assert calls[0] == ("en", "A I")


def test_single_english_route_honors_explicit_seed(monkeypatch):
    seeds = []

    def fake_synth_one(cfg, text, semantic_token_cb=None):
        seeds.append(cfg["seed"])
        return np.zeros(24000, dtype=np.float32), 24000

    monkeypatch.setattr(core.SYNTHESIS_BACKEND, "synth_one", fake_synth_one)
    core.SYNTHESIS_BACKEND.synth_segments(
        {"text_lang": "en", "speed": 1.0, "seed": 1234}, [("en", "URL")]
    )

    assert seeds[0] == 1234


def test_parser_keeps_repeated_same_language_fragments_separate():
    assert core.parse_language_segments("<en>Hello</en><en>world</en>") == [
        ("en", "Hello"),
        ("en", "world"),
    ]


def test_trim_segment_edges_handles_all_silence_and_integer_audio():
    silent = core.trim_segment_edges(np.zeros(80, dtype=np.float32), 24000)
    integer = core.trim_segment_edges(np.zeros(80, dtype=np.int16), 24000)

    assert silent.size > 0
    assert integer.size > 0


def test_same_language_segments_raise_on_inconsistent_sample_rates(monkeypatch):
    calls = []

    def fake_synth_one(cfg, text, semantic_token_cb=None):
        calls.append(text)
        return np.zeros(24000, dtype=np.float32), 24000 if len(calls) == 1 else 22050

    monkeypatch.setattr(core.SYNTHESIS_BACKEND, "synth_one", fake_synth_one)
    with pytest.raises(RuntimeError, match="采样率"):
        core.SYNTHESIS_BACKEND.synth_segments(
            {"text_lang": "zh"}, [("zh", "第一段"), ("zh", "第二段")]
        )


def test_deterministic_language_seed_is_stable_and_context_sensitive():
    cfg = {"ref_audio": "ref.wav", "ref_text": "参考", "gpt_model": "gpt", "sovits_model": "sovits"}

    same = core.deterministic_language_seed("auto", "你好 hello", cfg)
    assert same == core.deterministic_language_seed("auto", "你好 hello", cfg)
    assert same != core.deterministic_language_seed("auto_yue", "你好 hello", cfg)
    assert same != core.deterministic_language_seed("auto", "你好 world", cfg)
