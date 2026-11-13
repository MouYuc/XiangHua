from __future__ import annotations

import numpy as np

from tools import mixed_language_baseline_experiment as experiment

LANGUAGES = ("zh", "yue", "en", "ja", "ko")


def test_quality_matrix_covers_each_language_at_first_middle_and_last_position():
    cases = experiment.build_quality_cases()

    position_cases = [case for case in cases if case.category == "position"]
    assert len(position_cases) == 15
    for language in LANGUAGES:
        for position in ("first", "middle", "last"):
            matching = [
                case
                for case in position_cases
                if case.position == position
                and case.segments[case.target_index].lang == language
            ]
            assert len(matching) == 1
            case = matching[0]
            assert case.segments[case.target_index].lang == language
            assert len(case.segments) == 3


def test_quality_matrix_keeps_existing_baseline_cases_and_adds_alternating_orders():
    cases = experiment.build_quality_cases()
    case_ids = {case.case_id for case in cases}

    assert {
        "mixed_plain_zh_yue_en",
        "mixed_plain_zh_ja_ko",
        "mixed_plain_five_forward",
        "mixed_plain_five_reverse",
        "mixed_plain_alternating",
    } <= case_ids
    alternating = [case for case in cases if case.category == "alternating"]
    assert len(alternating) >= 2
    for case in alternating:
        assert len(case.segments) >= 5
        assert case.languages == tuple(segment.lang for segment in case.segments)


def test_real_usage_cases_are_five_independent_natural_scripts():
    cases = experiment.build_real_usage_cases()

    assert len(cases) == 5
    assert {case.category for case in cases} == {"real_usage"}
    assert all(case.languages == LANGUAGES for case in cases)
    assert all(sum(len(segment.text) for segment in case.segments) >= 80 for case in cases)
    assert all(not any(word in case.surface_text for word in experiment.FORBIDDEN_EXPANSIONS) for case in cases)


def test_real_usage_prefix_cases_add_languages_progressively():
    cases = experiment.build_real_usage_prefix_cases()

    assert len(cases) == 25
    for theme in experiment.REAL_USAGE_THEMES:
        theme_cases = [case for case in cases if case.theme == theme]
        assert [len(case.segments) for case in theme_cases] == [1, 2, 3, 4, 5]
        assert [case.languages for case in theme_cases] == [
            experiment.QUALITY_LANGUAGES[:index]
            for index in range(1, 6)
        ]
        assert all(case.category == "real_usage_prefix" for case in theme_cases)


def test_real_usage_short_cases_keep_five_languages_and_short_text_budget():
    cases = experiment.build_real_usage_short_cases()

    assert len(cases) == 15
    assert all(case.category == "real_usage_short" for case in cases)
    assert all(case.languages == experiment.QUALITY_LANGUAGES for case in cases)
    assert all(55 <= sum(len(segment.text) for segment in case.segments) <= 180 for case in cases)
    assert {case.short_index for case in cases} == {1, 2, 3}
    assert len({case.surface_text for case in cases}) == len(cases)


def test_real_usage_eos_probe_contains_only_reported_regressions():
    cases = experiment.build_real_usage_eos_probe_cases()

    assert [case.case_id for case in cases] == [
        "real_product_demo_prefix_5",
        "real_city_narration_prefix_5",
        "real_product_demo_short_1",
        "real_product_demo_short_3",
        "real_city_narration_short_1",
    ]


def test_boundary_waveform_metrics_measure_windows_from_phone_boundaries():
    sample_rate = 1000
    audio = np.zeros(1000, dtype=np.float32)
    audio[:400] = 0.5
    audio[600:] = 0.25
    boundaries = [
        {"index": 0, "lang": "zh", "start": 0, "end": 4},
        {"index": 1, "lang": "en", "start": 4, "end": 6},
        {"index": 2, "lang": "ja", "start": 6, "end": 10},
    ]

    metrics = experiment.boundary_waveform_metrics(
        audio, sample_rate, boundaries, phone_count=10, window_ms=100
    )

    assert len(metrics) == 2
    assert metrics[0]["boundary_phone"] == 4
    assert metrics[0]["boundary_sample"] == 400
    assert metrics[0]["pre_rms"] > metrics[0]["post_rms"]
    assert metrics[1]["boundary_sample"] == 600
    assert metrics[1]["post_rms"] > 0


def test_audio_quality_metrics_flag_nonfinite_and_clipping():
    audio = np.array([0.0, 0.5, 1.0, np.inf, np.nan], dtype=np.float32)

    metrics = experiment.audio_quality_metrics(audio)

    assert metrics == {
        "finite": False,
        "peak_abs": None,
        "rms": None,
        "clipped_ratio": None,
    }


def test_audio_quality_metrics_normalize_integer_pcm():
    metrics = experiment.audio_quality_metrics(np.array([0, 16384, -32768], dtype=np.int16))

    assert metrics["finite"] is True
    assert metrics["peak_abs"] == 1.0
    assert 0.6 < metrics["rms"] < 0.8
    assert metrics["clipped_ratio"] == 1 / 3


def test_pause_ablation_values_are_deterministic_and_include_baseline():
    assert experiment.PAUSE_ABLATION_MS == (0, 80, 120, 200)


def test_classify_eos_marks_mixed_cap_only_when_eos_was_not_sampled():
    assert experiment.classify_eos(
        final_token_index=249,
        duration=10.0,
        expected_duration=8.0,
        sampled_eos_observed=False,
        mixed_token_cap=250,
    )["hard_cap_hit"] is True
    assert experiment.classify_eos(
        final_token_index=249,
        duration=10.0,
        expected_duration=8.0,
        sampled_eos_observed=True,
        mixed_token_cap=250,
    )["hard_cap_hit"] is False
