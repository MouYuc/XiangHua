"""Run the D-group normal five-language mixed baseline."""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import gc
import hashlib
import io
import json
import math
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINE_ROOT = REPO_ROOT / "GPT-SoVITS"
PYTHON_EXE = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
DEFAULT_REF_AUDIO = ENGINE_ROOT / "custom_data" / "sliced" / "训练.wav_0000191040_0000343680.wav"
DEFAULT_REF_TEXT = "这条路走到头，左转就到了。"
DEFAULT_SEED = 424242
HARD_CAP_INDEX = 1499
BASELINE_GPT = "GPT_SoVITS/pretrained_models/s1v3.ckpt"
BASELINE_SOVITS = "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth"

DEFAULT_PARAMS: dict[str, Any] = {
    "top_k": 30,
    "top_p": 0.88,
    "temperature": 0.95,
    "sample_steps": 64,
    "speed_factor": 1.0,
    "fragment_interval": 0.15,
    "parallel_infer": False,
    "batch_size": 1,
    "text_split_method": "cut0",
}

FORBIDDEN_EXPANSIONS = (
    "Artificial Intelligence",
    "Application Programming Interface",
    "Generative Pre-trained Transformer",
    "Uniform Resource Locator",
    "National Aeronautics and Space Administration",
)


@dataclass(frozen=True)
class SegmentSpec:
    lang: str
    text: str


@dataclass(frozen=True)
class BaselineCase:
    case_id: str
    surface_text: str
    segments: tuple[SegmentSpec, ...]
    category: str = "mixed_baseline"
    position: str = ""
    target_index: int = -1
    pause_after_ms: int | None = None
    theme: str = ""
    short_index: int = 0

    @property
    def languages(self) -> tuple[str, ...]:
        return tuple(segment.lang for segment in self.segments)


def _case(
    case_id: str,
    surface: str,
    *segments: tuple[str, str],
    category: str = "mixed_baseline",
    position: str = "",
    target_index: int = -1,
    theme: str = "",
    short_index: int = 0,
) -> BaselineCase:
    return BaselineCase(
        case_id,
        surface,
        tuple(SegmentSpec(*segment) for segment in segments),
        category,
        position,
        target_index,
        None,
        theme,
        short_index,
    )


def build_baseline_cases() -> tuple[BaselineCase, ...]:
    return (
        _case("mixed_plain_zh_yue_en", "<zh>欢迎回来，</zh><yue>我记得佢今日喺屋企，</yue><en>the voice studio is ready.</en>", ("zh", "欢迎回来，"), ("yue", "我记得佢今日喺屋企，"), ("en", "the voice studio is ready.")),
        _case("mixed_plain_zh_ja_ko", "<zh>欢迎回来。</zh><ja>こんにちは。</ja><ko>안녕하세요。</ko>", ("zh", "欢迎回来。"), ("ja", "こんにちは。"), ("ko", "안녕하세요。")),
        _case("mixed_plain_five_forward", "<zh>现在开始。</zh><yue>我哋一齐行啦。</yue><en>the voice studio is ready.</en><ja>こんにちは。</ja><ko>안녕하세요。</ko>", ("zh", "现在开始。"), ("yue", "我哋一齐行啦。"), ("en", "the voice studio is ready."), ("ja", "こんにちは。"), ("ko", "안녕하세요。")),
        _case("mixed_plain_five_reverse", "<ko>안녕하세요。</ko><ja>こんにちは。</ja><en>the voice studio is ready.</en><yue>我哋一齐行啦。</yue><zh>现在开始。</zh>", ("ko", "안녕하세요。"), ("ja", "こんにちは。"), ("en", "the voice studio is ready."), ("yue", "我哋一齐行啦。"), ("zh", "现在开始。")),
        _case("mixed_plain_alternating", "<zh>现在</zh><en>the voice</en><yue>我记得佢</yue><ja>こんにちは</ja><ko>안녕하세요</ko><en>the studio is ready</en><zh>准备好了。</zh>", ("zh", "现在"), ("en", "the voice"), ("yue", "我记得佢"), ("ja", "こんにちは"), ("ko", "안녕하세요"), ("en", "the studio is ready"), ("zh", "准备好了。"), category="alternating"),
    )


QUALITY_LANGUAGES = ("zh", "yue", "en", "ja", "ko")
QUALITY_TEXT = {
    "zh": "现在开始。",
    "yue": "我哋一齐行啦。",
    "en": "the voice studio is ready.",
    "ja": "こんにちは。",
    "ko": "안녕하세요。",
}
PAUSE_ABLATION_MS = (0, 80, 120, 200)


def _tagged_surface(segments: tuple[SegmentSpec, ...]) -> str:
    return "".join(f"<{segment.lang}>{segment.text}</{segment.lang}>" for segment in segments)


def build_quality_cases() -> tuple[BaselineCase, ...]:
    """Build the fixed diagnostic matrix without loading the synthesis engine."""
    cases = list(build_baseline_cases())
    for language in QUALITY_LANGUAGES:
        anchors = [item for item in QUALITY_LANGUAGES if item != language][:2]
        for position in ("first", "middle", "last"):
            ordered = list(anchors)
            target_index = {"first": 0, "middle": 1, "last": 2}[position]
            ordered.insert(target_index, language)
            segments = tuple(SegmentSpec(item, QUALITY_TEXT[item]) for item in ordered)
            cases.append(
                BaselineCase(
                    case_id=f"position_{language}_{position}",
                    surface_text=_tagged_surface(segments),
                    segments=segments,
                    category="position",
                    position=position,
                    target_index=target_index,
                )
            )
    alternating_segments = tuple(
        SegmentSpec(item, QUALITY_TEXT[item])
        for item in ("ko", "en", "ja", "yue", "zh", "en", "ko")
    )
    cases.append(
        BaselineCase(
            case_id="mixed_plain_alternating_reverse",
            surface_text=_tagged_surface(alternating_segments),
            segments=alternating_segments,
            category="alternating",
        )
    )
    return tuple(cases)


def build_real_usage_cases() -> tuple[BaselineCase, ...]:
    """Five independent, production-shaped scripts for human listening review."""
    scripts = (
        (
            "real_product_demo",
            (
                ("zh", "今天我们来看看这款新的语音工作台，它可以把一段台词整理成清晰的配音任务。"),
                ("yue", "如果你想快啲开始，直接贴入台词就得喇。"),
                ("en", "Open the voice workspace and choose a scene."),
                ("ja", "画面が整ったら、そのまま確認してみてください。"),
                ("ko", "마지막으로 저장 버튼을 누르면 바로 결과를 들을 수 있습니다."),
            ),
        ),
        (
            "real_city_narration",
            (
                ("zh", "清晨的海风穿过老街，城市还没有完全醒来，路边的灯光正在一盏一盏熄灭。"),
                ("yue", "街坊慢慢开门，空气入面有阵阵面包香。"),
                ("en", "Beyond the station, the harbor begins to glow."),
                ("ja", "遠くから電車の音が静かに聞こえてきます。"),
                ("ko", "이제 하루가 천천히 시작됩니다."),
            ),
        ),
        (
            "real_software_tutorial",
            (
                ("zh", "先把参考音频放进工作区，再检查文本和语言标签是否一一对应。"),
                ("yue", "跟住撳入预览，听下每个转场位够唔够顺。"),
                ("en", "Select the preview button, then adjust the speaking speed."),
                ("ja", "問題がなければ、最後にファイルを保存します。"),
                ("ko", "오류가 보이면 설정을 바꾸기 전에 먼저 로그를 확인하세요."),
            ),
        ),
        (
            "real_drama_dialogue",
            (
                ("zh", "雨声压过了窗外的脚步声，他把那封信放在桌上，却没有立刻打开。"),
                ("yue", "你仲唔明咩？我等咗你成晚呀。"),
                ("en", "I thought you would never come back."),
                ("ja", "それでも、ここで待つしかなかった。"),
                ("ko", "이제는 더 이상 숨길 수 없어."),
            ),
        ),
        (
            "real_travel_service",
            (
                ("zh", "欢迎来到海湾站，请先确认行李和随身物品，再跟着地面的蓝色标线前进。"),
                ("yue", "如果要转车，喺前面个闸口出示张票就可以啦。"),
                ("en", "The next train leaves from platform three in ten minutes."),
                ("ja", "お困りのときは、近くの案内所までお越しください。"),
                ("ko", "즐거운 여행 되시고, 필요한 도움이 있으면 말씀해 주세요."),
            ),
        ),
    )
    return tuple(
        _case(
            case_id,
            _tagged_surface(tuple(SegmentSpec(lang, text) for lang, text in segments)),
            *segments,
            category="real_usage",
            theme=case_id,
        )
        for case_id, segments in scripts
    )


REAL_USAGE_THEMES = (
    "real_product_demo",
    "real_city_narration",
    "real_software_tutorial",
    "real_drama_dialogue",
    "real_travel_service",
)


def build_real_usage_prefix_cases() -> tuple[BaselineCase, ...]:
    """Build one-to-five-segment prefixes to locate the first unstable language."""
    cases = []
    for source in build_real_usage_cases():
        for count in range(1, 6):
            segments = source.segments[:count]
            cases.append(
                BaselineCase(
                    case_id=f"{source.case_id}_prefix_{count}",
                    surface_text=_tagged_surface(segments),
                    segments=segments,
                    category="real_usage_prefix",
                    theme=source.theme,
                    short_index=count,
                )
            )
    return tuple(cases)


_SHORT_USAGE_TEXT = {
    "real_product_demo": (
        (("zh", "工作台已经准备好了。"), ("yue", "而家可以开始喇。"), ("en", "The scene is ready."), ("ja", "確認して進みましょう。"), ("ko", "결과를 저장하세요.")),
        (("zh", "选择一个场景开始。"), ("yue", "贴入台词就得喇。"), ("en", "Choose a scene to record."), ("ja", "画面を確認してください。"), ("ko", "파일을 저장합니다.")),
        (("zh", "预览完成后再导出。"), ("yue", "听清楚先至保存。"), ("en", "Preview it before export."), ("ja", "最後に保存します。"), ("ko", "이제 재생해 보세요.")),
    ),
    "real_city_narration": (
        (("zh", "清晨的海风吹过老街。"), ("yue", "街坊慢慢开门啦。"), ("en", "The harbor is waking up."), ("ja", "遠くで電車が聞こえます。"), ("ko", "하루가 시작됩니다.")),
        (("zh", "灯光一盏一盏熄灭。"), ("yue", "空气入面有面包香。"), ("en", "The streets are still quiet."), ("ja", "朝の町が目を覚まします。"), ("ko", "도시는 천천히 깨어납니다.")),
        (("zh", "第一班车驶过河岸。"), ("yue", "海边开始热闹起来。"), ("en", "Morning light reaches the water."), ("ja", "新しい一日が始まります。"), ("ko", "새로운 아침이 열립니다.")),
    ),
    "real_software_tutorial": (
        (("zh", "请检查文本和参考音频。"), ("yue", "跟住听下转场位。"), ("en", "Adjust the speed if needed."), ("ja", "問題がなければ保存します。"), ("ko", "오류가 있으면 로그를 확인하세요.")),
        (("zh", "先确认语言标签。"), ("yue", "再撳入预览按钮。"), ("en", "Select the preview button."), ("ja", "設定を確認してください。"), ("ko", "먼저 로그를 확인하세요.")),
        (("zh", "听起来顺就可以导出。"), ("yue", "有问题就再试一次。"), ("en", "Export the file when ready."), ("ja", "最後にファイルを保存します。"), ("ko", "완료되면 파일을 저장하세요.")),
    ),
    "real_drama_dialogue": (
        (("zh", "雨声盖住了脚步声。"), ("yue", "你仲唔明咩？"), ("en", "I thought you would come back."), ("ja", "ここで待つしかなかった。"), ("ko", "더 이상 숨길 수 없어.")),
        (("zh", "他把信放在桌上。"), ("yue", "我等咗你成晚呀。"), ("en", "You should have told me."), ("ja", "それでも話せませんでした。"), ("ko", "이제 진실을 말할게.")),
        (("zh", "房间里只剩下雨声。"), ("yue", "我唔会再等喇。"), ("en", "This is our last chance."), ("ja", "もう戻れないでしょう。"), ("ko", "다시는 돌아갈 수 없어.")),
    ),
    "real_travel_service": (
        (("zh", "欢迎来到海湾站。"), ("yue", "转车就喺前面。"), ("en", "The next train leaves soon."), ("ja", "案内所までお越しください。"), ("ko", "즐거운 여행 되세요.")),
        (("zh", "请确认行李和车票。"), ("yue", "跟住蓝色标线行啦。"), ("en", "Platform three is on the left."), ("ja", "駅員にお尋ねください。"), ("ko", "필요하면 말씀해 주세요.")),
        (("zh", "列车十分钟后进站。"), ("yue", "准备好就可以出发喇。"), ("en", "Please keep your ticket ready."), ("ja", "足元にご注意ください。"), ("ko", "안전하게 이동하세요.")),
    ),
}


def build_real_usage_short_cases() -> tuple[BaselineCase, ...]:
    """Build three concise five-language scripts for each real-use theme."""
    cases = []
    for theme in REAL_USAGE_THEMES:
        for short_index, rows in enumerate(_SHORT_USAGE_TEXT[theme], start=1):
            segments = tuple(SegmentSpec(lang, text) for lang, text in rows)
            cases.append(
                BaselineCase(
                    case_id=f"{theme}_short_{short_index}",
                    surface_text=_tagged_surface(segments),
                    segments=segments,
                    category="real_usage_short",
                    theme=theme,
                    short_index=short_index,
                )
            )
    return tuple(cases)


def build_real_usage_eos_probe_cases() -> tuple[BaselineCase, ...]:
    """Keep the EOS trial focused on samples with reported tail/omission issues."""
    wanted = {
        "real_product_demo_prefix_5",
        "real_city_narration_prefix_5",
        "real_product_demo_short_1",
        "real_product_demo_short_3",
        "real_city_narration_short_1",
    }
    cases = build_real_usage_prefix_cases() + build_real_usage_short_cases()
    return tuple(case for case in cases if case.case_id in wanted)


def _normalized_audio(audio: Any):
    import numpy as np

    values = np.asarray(audio)
    if np.issubdtype(values.dtype, np.integer):
        info = np.iinfo(values.dtype)
        scale = float(max(abs(info.min), info.max))
        return values.astype(np.float64, copy=False) / scale
    return values.astype(np.float64, copy=False)


def audio_quality_metrics(audio: Any) -> dict[str, Any]:
    """Return finite/peak/RMS checks used for screening, not pronunciation scoring."""
    import numpy as np

    values = _normalized_audio(audio)
    if values.size == 0 or not bool(np.isfinite(values).all()):
        return {"finite": False, "peak_abs": None, "rms": None, "clipped_ratio": None}
    magnitude = np.abs(values)
    return {
        "finite": True,
        "peak_abs": float(magnitude.max()),
        "rms": float(np.sqrt(np.mean(np.square(values)))),
        "clipped_ratio": float(np.mean(magnitude >= 0.999)),
    }


def boundary_waveform_metrics(
    audio: Any,
    sample_rate: int,
    boundaries: list[dict[str, Any]],
    *,
    phone_count: int,
    window_ms: int = 200,
) -> list[dict[str, Any]]:
    """Measure RMS/silence around phone-proportional boundaries as coarse proxies."""
    import numpy as np

    values = _normalized_audio(audio).astype(np.float32, copy=False)
    if values.size == 0 or phone_count <= 0 or sample_rate <= 0:
        return []
    window = max(1, int(round(sample_rate * window_ms / 1000)))
    metrics = []
    for boundary in boundaries[:-1]:
        boundary_phone = int(boundary.get("end", 0))
        sample = int(round(boundary_phone / phone_count * len(values)))
        sample = max(0, min(len(values), sample))
        pre = values[max(0, sample - window) : sample]
        post = values[sample : min(len(values), sample + window)]
        if pre.size == 0 or post.size == 0:
            continue
        pre_rms = float(np.sqrt(np.mean(np.square(pre.astype(np.float64)))))
        post_rms = float(np.sqrt(np.mean(np.square(post.astype(np.float64)))))
        metrics.append(
            {
                "boundary_index": int(boundary.get("index", 0)),
                "boundary_phone": boundary_phone,
                "boundary_sample": sample,
                "pre_rms": pre_rms,
                "post_rms": post_rms,
                "pre_silence_ratio": float(np.mean(np.abs(pre) < 0.01)),
                "post_silence_ratio": float(np.mean(np.abs(post) < 0.01)),
            }
        )
    return metrics


def surface_preserved(surface_text: str, pronunciation_text: str) -> bool:
    return bool(surface_text.strip()) and not any(item in pronunciation_text for item in FORBIDDEN_EXPANSIONS)


def classify_eos(
    *,
    final_token_index: int,
    duration: float,
    expected_duration: float,
    sampled_eos_observed: bool = False,
    mixed_token_cap: int | None = None,
) -> dict[str, bool]:
    cap_index = int(mixed_token_cap) - 1 if mixed_token_cap is not None else HARD_CAP_INDEX
    hard_cap_hit = not sampled_eos_observed and int(final_token_index) >= cap_index
    ratio = float(duration) / float(expected_duration) if expected_duration > 0 else 1.0
    return {"hard_cap_hit": hard_cap_hit, "eos_before_hard_cap": not hard_cap_hit, "premature_eos_suspect": (not hard_cap_hit) and ratio < 0.80}


def _checkpoint_load(path: Path) -> dict[str, Any]:
    import torch

    raw = path.read_bytes()
    if raw[:2] != b"PK":
        raw = b"PK" + raw[2:]
    return torch.load(io.BytesIO(raw), map_location="cpu", weights_only=False)


def checkpoint_metadata(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"模型文件不存在：{path}")
    raw = path.read_bytes()
    data = _checkpoint_load(path)
    config = data.get("config", {})
    model_config = config.get("model", {}) if isinstance(config, dict) else {}
    return {"path": str(path), "size": path.stat().st_size, "head_hex": raw[:2].hex(), "md5_first_8192": hashlib.md5(raw[:8192]).hexdigest(), "config_version": model_config.get("version") if hasattr(model_config, "get") else None, "sampling_rate": config.get("data", {}).get("sampling_rate") if isinstance(config, dict) else None, "semantic_frame_rate": model_config.get("semantic_frame_rate") if hasattr(model_config, "get") else None}


def validate_environment() -> dict[str, Any]:
    required = {"python": PYTHON_EXE, "engine_root": ENGINE_ROOT, "ref_audio": DEFAULT_REF_AUDIO, "bert": ENGINE_ROOT / "GPT_SoVITS" / "pretrained_models" / "chinese-roberta-wwm-ext-large", "hubert": ENGINE_ROOT / "GPT_SoVITS" / "pretrained_models" / "chinese-hubert-base"}
    missing = [f"{name}: {path}" for name, path in required.items() if not path.exists()]
    if missing:
        raise FileNotFoundError("实验环境缺少：" + "；".join(missing))
    return {"required": {name: str(path) for name, path in required.items()}, "model": {"label": "D", "gpt": checkpoint_metadata(ENGINE_ROOT / BASELINE_GPT), "sovits": checkpoint_metadata(ENGINE_ROOT / BASELINE_SOVITS)}}


def engine_import_paths() -> list[str]:
    return [str(REPO_ROOT), str(ENGINE_ROOT), str(ENGINE_ROOT / "GPT_SoVITS"), str(ENGINE_ROOT / "GPT_SoVITS" / "eres2net")]


def _jsonable_audio(audio: Any):
    import numpy as np

    if hasattr(audio, "detach"):
        audio = audio.detach().cpu().numpy()
    return np.asarray(audio)


def _install_capture_hooks(tts: Any, state: dict[str, Any]) -> None:
    original_preprocess = tts.text_preprocessor.preprocess_segments

    def preprocess(segments, version="v2"):
        data = original_preprocess(segments, version)
        state["preprocessed"] = data
        return data

    tts.text_preprocessor.preprocess_segments = preprocess
    model = tts.t2s_model.model
    original_infer = model.infer_panel_naive_batched

    def infer(*args, **kwargs):
        result = original_infer(*args, **kwargs)
        state["final_token_indices"] = [int(value) for value in result[1]]
        return result

    model.infer_panel_naive_batched = infer
    original_decode = tts.vits_model.decode

    def decode(*args, **kwargs):
        state["acoustic_decode_calls"] = int(state.get("acoustic_decode_calls", 0)) + 1
        return original_decode(*args, **kwargs)

    tts.vits_model.decode = decode
    infer_globals = getattr(model.infer_panel_naive, "__func__", model.infer_panel_naive).__globals__
    hook_state = getattr(model, "_codex_eos_hook_state", None)
    if hook_state is None:
        original_sample = infer_globals.get("sample")
        if original_sample is not None:
            hook_state = {"state": state, "eos_id": int(model.EOS)}

            def sample(*args, **kwargs):
                result = original_sample(*args, **kwargs)
                values = result[0] if isinstance(result, (tuple, list)) else result
                try:
                    if bool((values == hook_state["eos_id"]).any().item()):
                        hook_state["state"]["sampled_eos_observed"] = True
                except (AttributeError, RuntimeError, TypeError):
                    pass
                return result

            infer_globals["sample"] = sample
            model._codex_eos_hook_state = hook_state
    if hook_state is not None:
        hook_state["state"] = state


def _engine_segments(case: BaselineCase) -> list[dict[str, Any]]:
    return _engine_segments_with_pause(case)


def _engine_segments_with_pause(
    case: BaselineCase, boundary_pause_ms: int | None = None
) -> list[dict[str, Any]]:
    from backend.services.core import MIXED_LANGUAGE_BOUNDARY_PAUSE_MS, to_engine_segments

    pause_ms = (
        case.pause_after_ms
        if case.pause_after_ms is not None
        else boundary_pause_ms
        if boundary_pause_ms is not None
        else MIXED_LANGUAGE_BOUNDARY_PAUSE_MS
    )
    segments = to_engine_segments(
        [(item.lang, item.text) for item in case.segments], boundary_pause_ms=pause_ms
    )
    for segment in segments:
        if not surface_preserved(segment["text"], segment.get("pronunciation_text", "")):
            raise ValueError(f"表面文本被展开：{segment}")
    return segments


def run_worker(
    output_root: Path,
    seed: int,
    device: str,
    is_half: bool,
    cases: tuple[BaselineCase, ...] | None = None,
    boundary_pause_ms: int | None = None,
    mixed_eos_floor: int | None = None,
) -> int:
    import numpy as np
    import soundfile as sf
    import torch

    pair_root = output_root / "D"
    audio_root = pair_root / "audio"
    audio_root.mkdir(parents=True, exist_ok=True)
    log_path = pair_root / "engine.log"
    manifest_path = pair_root / "manifest.jsonl"
    cfg = {"device": device, "is_half": bool(is_half), "version": "v2Pro", "t2s_weights_path": str((ENGINE_ROOT / BASELINE_GPT).resolve()), "vits_weights_path": str((ENGINE_ROOT / BASELINE_SOVITS).resolve()), "cnhuhbert_base_path": "GPT_SoVITS/pretrained_models/chinese-hubert-base", "bert_base_path": "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large"}
    for index, import_path in enumerate(engine_import_paths()):
        if import_path not in sys.path:
            sys.path.insert(index, import_path)
    import tools as project_tools

    engine_tools_path = str(ENGINE_ROOT / "tools")
    project_tools_paths = getattr(project_tools, "__path__", None)
    if project_tools_paths is not None and engine_tools_path not in project_tools_paths:
        project_tools_paths.append(engine_tools_path)
    from GPT_SoVITS.TTS_infer_pack.TTS import TTS, TTS_Config

    from backend.services.core import expected_segment_duration

    with log_path.open("w", encoding="utf-8") as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        tts = TTS(TTS_Config({"custom": cfg}))
    (pair_root / "load.json").write_text(json.dumps({"pair": "D", "gpt_model": cfg["t2s_weights_path"], "sovits_model": cfg["vits_weights_path"], "version": tts.configs.version, "is_v2pro": bool(tts.is_v2pro), "sampling_rate": int(tts.configs.sampling_rate)}, ensure_ascii=False, indent=2), encoding="utf-8")
    with manifest_path.open("w", encoding="utf-8") as manifest, log_path.open("a", encoding="utf-8") as log:
        for case in cases or build_baseline_cases():
            state: dict[str, Any] = {"preprocessed": None, "final_token_indices": [], "acoustic_decode_calls": 0, "sampled_eos_observed": False, "semantic_token_count": 0, "semantic_frame_rate": 0}
            engine_segments = _engine_segments_with_pause(case, boundary_pause_ms)
            expected_duration = sum(expected_segment_duration(item["pronunciation_text"], item["lang"], DEFAULT_PARAMS["speed_factor"]) for item in engine_segments)

            def semantic_callback(count: int, frame_rate: int = 0) -> None:
                state["semantic_token_count"] = max(state["semantic_token_count"], int(count))
                state["semantic_frame_rate"] = max(state["semantic_frame_rate"], int(frame_rate))

            torch.manual_seed(seed)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(seed)
            request = {"text": "", "text_lang": "segment_mixed", "text_segments": engine_segments, "ref_audio_path": str(DEFAULT_REF_AUDIO), "prompt_text": DEFAULT_REF_TEXT, "prompt_lang": "zh", **DEFAULT_PARAMS, "seed": seed, "return_fragment": False, "streaming_mode": False, "semantic_token_callback": semantic_callback}
            if mixed_eos_floor is not None:
                request["mixed_eos_floor"] = int(mixed_eos_floor)
            _install_capture_hooks(tts, state)
            error = ""
            duration = 0.0
            sample_rate = 0
            audio_path = audio_root / f"{case.case_id}.wav"
            try:
                with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                    result = list(tts.run(request))
                sample_rate, audio = result[-1]
                sample_rate = int(sample_rate)
                audio = _jsonable_audio(audio)
                duration = float(len(audio)) / sample_rate if sample_rate else 0.0
                sf.write(audio_path, np.asarray(audio), sample_rate)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            item = (state.get("preprocessed") or [{}])[0]
            final_index = max(state["final_token_indices"], default=-1)
            phone_count = len(item.get("phones", []))
            mixed_token_cap = max(
                int(mixed_eos_floor) if mixed_eos_floor is not None else 11,
                max(11, int(math.ceil(phone_count * 2.5))),
            )
            waveform = audio_quality_metrics(audio) if not error else {
                "finite": False,
                "peak_abs": None,
                "rms": None,
                "clipped_ratio": None,
            }
            boundary_metrics = (
                boundary_waveform_metrics(
                    audio,
                    sample_rate,
                    item.get("segment_boundaries", []),
                    phone_count=phone_count,
                )
                if not error
                else []
            )
            record = {
                "pair": "D",
                "case_id": case.case_id,
                "category": case.category,
                "position": case.position,
                "target_index": case.target_index,
                "theme": case.theme,
                "short_index": case.short_index,
                "pause_after_ms": case.pause_after_ms if case.pause_after_ms is not None else boundary_pause_ms,
                "surface_text": case.surface_text,
                "segments": [asdict(segment) for segment in case.segments],
                "engine_segments": engine_segments,
                "started_at": _dt.datetime.now().isoformat(timespec="seconds"),
                "audio_file": str(audio_path) if not error else "",
                "sample_rate": sample_rate,
                "duration_sec": duration,
                "expected_duration_sec": expected_duration,
                "phone_ids": [int(value) for value in item.get("phones", [])],
                "phone_count": phone_count,
                "mixed_token_cap": mixed_token_cap,
                "norm_text": item.get("norm_text", ""),
                "segment_boundaries": item.get("segment_boundaries", []),
                "boundary_waveform_metrics": boundary_metrics,
                "semantic_token_count": state["semantic_token_count"],
                "semantic_frame_rate": state["semantic_frame_rate"],
                "final_token_index": final_index,
                "sampled_eos_observed": bool(state["sampled_eos_observed"]),
                **classify_eos(
                    final_token_index=final_index,
                    duration=duration,
                    expected_duration=expected_duration,
                    sampled_eos_observed=bool(state["sampled_eos_observed"]),
                    mixed_token_cap=mixed_token_cap,
                ),
                **waveform,
                "inference_calls": 1 if not error else 0,
                "acoustic_decode_calls": int(state["acoustic_decode_calls"]),
                "error": error,
                "manual": {
                    "omission_score": None,
                    "boundary_clarity_score": None,
                    "language_switch_naturalness_score": None,
                    "chinese_timbre_prosody_score": None,
                    "notes": "",
                },
            }
            manifest.write(json.dumps(record, ensure_ascii=False) + "\n")
            manifest.flush()
            status = "FAIL " + error if error else f"OK {duration:.2f}s phones={record['phone_count']} tokens={record['semantic_token_count']}"
            print(f"D {case.case_id}: {status}")
    del tts
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    return 1 if any(json.loads(line).get("error") for line in manifest_path.read_text(encoding="utf-8").splitlines()) else 0


def _write_listening_page(output_root: Path) -> None:
    manifest = output_root / "D" / "manifest.jsonl"
    rows = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
    items = []
    for row in rows:
        if row.get("audio_file"):
            items.append(f"<tr><td>{row['case_id']}</td><td>{row['surface_text']}</td><td>{row['duration_sec']:.2f}s / {row['semantic_token_count']} tokens</td><td>{row['premature_eos_suspect']}</td><td><audio controls src='D/audio/{Path(row['audio_file']).name}'></audio></td></tr>")
    html = "<!doctype html><meta charset='utf-8'><title>Mixed language baseline</title><style>body{font:15px system-ui,sans-serif;max-width:1200px;margin:24px auto;padding:0 16px}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ddd;padding:8px;text-align:left}audio{width:260px}</style><h1>五语种正常混合基线</h1><p>每条样本一次 segment_mixed 推理；不含缩略词。</p><table><thead><tr><th>用例</th><th>原文</th><th>时长 / token</th><th>提前 EOS</th><th>音频</th></tr></thead><tbody>" + "".join(items) + "</tbody></table>"
    (output_root / "listening.html").write_text(html, encoding="utf-8")


def run_baseline(output_root: Path, seed: int, device: str, is_half: bool) -> int:
    return run_experiment(output_root, seed, device, is_half, mode="baseline")


def _pause_ablation_cases() -> tuple[BaselineCase, ...]:
    source = {case.case_id: case for case in build_baseline_cases()}
    selected = [
        source["mixed_plain_five_forward"],
        source["mixed_plain_five_reverse"],
        source["mixed_plain_alternating"],
    ]
    return tuple(
        BaselineCase(
            case_id=f"{case.case_id}_pause_{pause_ms}",
            surface_text=case.surface_text,
            segments=case.segments,
            category="pause_ablation",
            position=case.position,
            target_index=case.target_index,
            pause_after_ms=pause_ms,
        )
        for case in selected
        for pause_ms in PAUSE_ABLATION_MS
    )


def run_experiment(
    output_root: Path,
    seed: int,
    device: str,
    is_half: bool,
    *,
    mode: str,
    mixed_eos_floor: int | None = None,
) -> int:
    output_root = Path(output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    preflight = validate_environment()
    (output_root / "preflight.json").write_text(json.dumps(preflight, ensure_ascii=False, indent=2), encoding="utf-8")
    if mode == "baseline":
        cases = build_baseline_cases()
    elif mode == "matrix":
        cases = build_quality_cases()
    elif mode == "pause":
        cases = _pause_ablation_cases()
    elif mode == "real":
        cases = build_real_usage_cases()
    elif mode == "prefix":
        cases = build_real_usage_prefix_cases()
    elif mode == "short":
        cases = build_real_usage_short_cases()
    elif mode == "eos_probe":
        cases = build_real_usage_eos_probe_cases()
    elif mode == "all":
        cases = build_quality_cases() + _pause_ablation_cases()
    else:
        raise ValueError(f"未知实验模式：{mode}")
    (output_root / "run_config.json").write_text(json.dumps({"created_at": _dt.datetime.now().isoformat(timespec="seconds"), "seed": seed, "device": device, "is_half": is_half, "pair": "D", "gpt_model": str((ENGINE_ROOT / BASELINE_GPT).resolve()), "sovits_model": str((ENGINE_ROOT / BASELINE_SOVITS).resolve()), "ref_audio": str(DEFAULT_REF_AUDIO), "ref_text": DEFAULT_REF_TEXT, "text_lang": "segment_mixed", "single_pass_required": True, "mode": mode, "case_count": len(cases), "mixed_eos_floor": mixed_eos_floor, "mixed_eos_default_ratio": 1.5, "mixed_token_cap_ratio": 2.5, "pause_ablation_ms": list(PAUSE_ABLATION_MS) if mode in {"pause", "all"} else []}, ensure_ascii=False, indent=2), encoding="utf-8")
    previous_cwd = os.getcwd()
    try:
        os.chdir(ENGINE_ROOT)
        status = run_worker(output_root, seed, device, is_half, cases=cases, mixed_eos_floor=mixed_eos_floor)
    finally:
        os.chdir(previous_cwd)
    _write_listening_page(output_root)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--is-half", type=int, choices=(0, 1), default=1)
    parser.add_argument("--mode", choices=("baseline", "matrix", "pause", "real", "prefix", "short", "eos_probe", "all"), default="baseline")
    parser.add_argument("--mixed-eos-floor", type=int, default=None)
    args = parser.parse_args()
    default_root = "mixed_language_baseline" if args.mode == "baseline" else "mixed_language_quality"
    output = args.output_root or REPO_ROOT / "experiments" / default_root / _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return run_experiment(output, args.seed, args.device, bool(args.is_half), mode=args.mode, mixed_eos_floor=args.mixed_eos_floor)


if __name__ == "__main__":
    raise SystemExit(main())
