import numpy as np

from backend.services import core


def test_build_engine_segments_keeps_explicit_language_order():
    cfg = {"text_lang": "zh", "auto_lightmid": True}
    segments = core.render_language_segments(
        core.parse_language_segments(
            "<zh>你好</zh><yue>我记得佢</yue><en>AI Voice</en><ja>こんにちは</ja><ko>안녕하세요</ko>"
        ),
        cfg,
    )

    assert [(item["lang"], item["text"]) for item in core.to_engine_segments(segments)] == [
        ("zh", "你好"),
        ("yue", "我记得佢"),
        ("en", "AI Voice"),
        ("ja", "こんにちは"),
        ("ko", "안녕하세요"),
    ]

    engine_segments = core.to_engine_segments([("en", "AI Voice")])
    assert engine_segments[0]["text"] == "AI Voice"
    assert engine_segments[0]["pronunciation_text"] == "A I, Voice"


def test_mixed_request_uses_segment_mixed_and_forwards_segments(monkeypatch):
    captured = []

    def fake_synth_one(cfg, text, semantic_token_cb=None, text_segments=None):
        captured.append((cfg["text_lang"], text, text_segments))
        return np.zeros(10, dtype=np.float32), 24000

    backend = core.SynthesisBackend()
    monkeypatch.setattr(backend, "synth_one", fake_synth_one)
    backend.synth_segments(
        {"text_lang": "zh", "auto_lightmid": False},
        [("zh", "你好"), ("en", "Hello")],
    )

    assert len(captured) == 1
    assert captured[0][0] == "segment_mixed"
    assert [(item["lang"], item["text"]) for item in captured[0][2]] == [
        ("zh", "你好"),
        ("en", "Hello"),
    ]


def test_mixed_request_marks_only_language_boundaries_with_light_pause(monkeypatch):
    captured = []

    def fake_synth_one(cfg, text, semantic_token_cb=None, text_segments=None):
        captured.append(text_segments)
        return np.zeros(10, dtype=np.float32), 24000

    backend = core.SynthesisBackend()
    monkeypatch.setattr(backend, "synth_one", fake_synth_one)
    backend.synth_segments(
        {"text_lang": "zh", "auto_lightmid": False},
        [("zh", "你好"), ("yue", "我记得佢"), ("en", "Hello")],
    )

    assert [item["pause_after_ms"] for item in captured[0]] == [120, 120, 0]


def test_five_language_mix_is_one_request_with_order_and_final_punctuation(monkeypatch):
    captured = []

    def fake_synth_one(cfg, text, semantic_token_cb=None, text_segments=None):
        captured.append((cfg, text, text_segments))
        return np.zeros(10, dtype=np.float32), 24000

    backend = core.SynthesisBackend()
    monkeypatch.setattr(backend, "synth_one", fake_synth_one)
    backend.synth_segments(
        {"text_lang": "zh", "auto_lightmid": False},
        [
            ("zh", "普通话。"),
            ("yue", "粤语！"),
            ("en", "English."),
            ("ja", "日本語？"),
            ("ko", "한국어."),
        ],
    )

    assert len(captured) == 1
    cfg, text, segments = captured[0]
    assert cfg["text_lang"] == "segment_mixed"
    assert text == ""
    assert [item["lang"] for item in segments] == ["zh", "yue", "en", "ja", "ko"]
    assert [item["text"] for item in segments] == ["普通话。", "粤语！", "English.", "日本語？", "한국어."]
    assert [item["pronunciation_text"] for item in segments] == ["普通话", "粤语", "English", "日本語", "한국어."]
    assert [item["pause_after_ms"] for item in segments] == [120, 120, 120, 120, 0]


def test_engine_segments_preserve_surface_text_but_normalize_nonfinal_terminal_punctuation():
    surface_segments = [
        ("zh", "第一句。"),
        ("yue", "第二句！"),
        ("en", "调用 API."),
        ("ja", "第四句？"),
        ("ko", "最后一句."),
    ]

    engine_segments = core.to_engine_segments(
        surface_segments,
        boundary_pause_ms=core.MIXED_LANGUAGE_BOUNDARY_PAUSE_MS,
    )

    assert [item["text"] for item in engine_segments] == [item[1] for item in surface_segments]
    assert [item["pronunciation_text"] for item in engine_segments] == [
        "第一句",
        "第二句",
        "调用 A P I",
        "第四句",
        "最后一句.",
    ]
    assert [item["lang"] for item in engine_segments] == ["zh", "yue", "en", "ja", "ko"]
    assert [item["pause_after_ms"] for item in engine_segments] == [120, 120, 120, 120, 0]


def test_synth_one_forwards_optional_mixed_eos_floor(tmp_path):
    captured = []

    class FakePipeline:
        def run(self, request):
            captured.append(request)
            return [(24000, np.zeros(10, dtype=np.float32))]

    backend = core.SynthesisBackend()
    backend._pipeline = FakePipeline()
    cfg = {
        "gpt_sovits_root": str(tmp_path),
        "ref_audio": "ref.wav",
        "ref_text": "参考",
        "text_lang": "segment_mixed",
        "prompt_lang": "zh",
        "top_k": 30,
        "top_p": 0.88,
        "temperature": 0.95,
        "speed": 1.0,
        "pause_second": 0.15,
        "sample_steps": 64,
        "mixed_eos_floor": 11,
        "mixed_token_cap": 240,
    }

    backend.synth_one(cfg, "", text_segments=[{"lang": "zh", "text": "你好"}])

    assert captured[0]["mixed_eos_floor"] == 11
    assert captured[0]["mixed_token_cap"] == 240


def test_synth_one_sends_one_explicit_engine_request(tmp_path):
    captured = []

    class FakePipeline:
        def run(self, request):
            captured.append(request)
            return [(24000, np.zeros(10, dtype=np.float32))]

    backend = core.SynthesisBackend()
    backend._pipeline = FakePipeline()
    segments = [
        {"lang": "zh", "text": "你好", "pause_after_ms": 0},
        {"lang": "yue", "text": "我记得佢", "pause_after_ms": 0},
        {"lang": "en", "text": "Hello", "pause_after_ms": 0},
        {"lang": "ja", "text": "こんにちは", "pause_after_ms": 0},
        {"lang": "ko", "text": "안녕하세요", "pause_after_ms": 0},
    ]
    cfg = {
        "gpt_sovits_root": str(tmp_path),
        "ref_audio": "ref.wav",
        "ref_text": "参考",
        "text_lang": "zh",
        "prompt_lang": "zh",
        "top_k": 30,
        "top_p": 0.88,
        "temperature": 0.95,
        "speed": 1.0,
        "pause_second": 0.15,
        "sample_steps": 64,
    }

    backend.synth_one(cfg, "", text_segments=segments)

    assert len(captured) == 1
    assert captured[0]["text_lang"] == "segment_mixed"
    assert [item["lang"] for item in captured[0]["text_segments"]] == ["zh", "yue", "en", "ja", "ko"]


def test_synthesize_lines_reports_explicit_single_pass_metadata(monkeypatch, tmp_path):
    progress = []
    monkeypatch.setattr(core.SYNTHESIS_BACKEND, "ensure", lambda cfg: None)
    monkeypatch.setattr(
        core.SYNTHESIS_BACKEND,
        "synth_segments",
        lambda cfg, segments, semantic_token_cb=None: (np.zeros(10, dtype=np.float32), 24000),
    )
    monkeypatch.setattr(core, "register_download", lambda path: "download")
    monkeypatch.setattr(core, "append_history", lambda entries: None)

    result = core.synthesize_lines(
        {
            "text_lang": "zh",
            "prompt_lang": "zh",
            "auto_lightmid": False,
            "temperature": 0.95,
            "sample_steps": 64,
        },
        "<zh>你好</zh><yue>我记得佢</yue><en>Hello</en><ja>こんにちは</ja><ko>안녕하세요</ko>",
        output_dir=tmp_path,
        progress_cb=progress.append,
    )

    generating = next(item for item in progress if item["phase"] == "generating")
    writing = next(item for item in progress if item["phase"] == "writing")
    done = progress[-1]
    expected = {
        "synthesis_mode": "single_pass_explicit",
        "segment_count": 5,
        "acoustic_inference_calls": 1,
    }
    assert {key: generating[key] for key in expected} == {**expected, "acoustic_inference_calls": 0}
    assert {key: writing[key] for key in expected} == expected
    assert {key: done[key] for key in expected} == expected
    assert {key: result[key] for key in expected} == expected
