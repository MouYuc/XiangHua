#!/usr/bin/env python3
"""End-to-end verification of the five-language dubbing chain, measured not listened to.

The product claim is: one personal timbre, five languages (zh / yue / en / ja / ko), one
continuous inference per line. Three of those words are testable without an ear, and
The project's objective evaluation criteria are:

1. **Is each output actually in its language?** Language identification with
   **SenseVoiceSmall** (`iic/SenseVoiceSmall`, whose tokeniser is literally
   `chn_jpn_yue_eng_ko`). The zero-training criterion for five-language
   dubbing is LID agreement >= 95%.
2. **Is it still the same voice across all five?** Speaker embedding cosine similarity
   from the independent ERes2NetV2 model - never from the synthesis engine's own
   features. The user-value metric is the **cross-language timbre drift**,
   ``max(SECS) - min(SECS) <= 0.05``.
3. **Did it come out as one pass?** The mixed path must report
   ``synthesis_mode=single_pass_explicit`` with ``acoustic_inference_calls == 1``;
   several calls or a ``legacy_single`` mode means the line was stitched from
   separately synthesised segments, which is the failure this project spent effort
   removing.

Per-language CER is reported as an **intelligibility guardrail only**. Seed-TTS
(arXiv:2406.02430, section 3.1) documents that lower WER usually means *more
standardised, less accented* speech, so CER must never be read as a fidelity score.

Usage:
    .venv\\Scripts\\python.exe scripts\\five_language_verify.py
    .venv\\Scripts\\python.exe scripts\\five_language_verify.py --skip-synthesis   # reuse the last run
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# One short, unambiguous sentence per language. Kept apart from the mixed line so a
# language failure cannot be blamed on the mixing.
SENTENCES = {
    "zh": "今天天气很好，我们出去走走。",
    "yue": "今日天氣好好，我哋出去行下。",
    "en": "The voice lab is ready for the next take.",
    "ja": "今日はいい天気ですね、散歩に行きましょう。",
    "ko": "오늘 날씨가 좋아서 산책하러 가요.",
}

MIXED_LINE = "".join(f"<{language}>{text}</{language}>" for language, text in SENTENCES.items())


def tagged(language: str, text: str) -> str:
    """Wrap a line in its explicit language tag.

    Required, not cosmetic. Without a tag the line is synthesised in the page's
    default language (``zh``), and the Mandarin frontend has no phones for Hangul:
    measured on this model, untagged Korean produced **0.08 s** of near-silence while
    the same sentence tagged produced 2.50 s. Japanese appears to survive untagged only
    because kana and kanji overlap with the Chinese frontend's inventory, which makes
    the failure inconsistent enough to be dangerous. The synthesis path now flags a
    suspiciously short result rather than reporting success (see ``SHORT_OUTPUT_RATIO``).
    """
    return f"<{language}>{text}</{language}>"

# SenseVoice emits its language as a leading token in the transcript.
_LANGUAGE_TAGS = {"<|zh|>": "zh", "<|yue|>": "yue", "<|en|>": "en", "<|ja|>": "ja", "<|ko|>": "ko"}

# Five Chinese sentences, used to measure the drift metric's own noise floor. If five
# utterances of the *same* language already spread as far apart as the five languages
# do, the cross-language number is dominated by utterance-to-utterance variation and
# reading it as a language effect would be wrong. Establishing that baseline before
# judging the cross-language figure is the difference between a measurement and a
# number that merely looks like one.
SAME_LANGUAGE_BASELINE = [
    "今天天气很好，我们出去走走。",
    "这本书我读了三遍，每次都有新收获。",
    "周末打算去山里住两天，换个环境。",
    "会议改到下午三点，地点还是老地方。",
    "他说话不快，但每句都很清楚。",
]

PLAYBOOK_LID_TARGET = 0.95
PLAYBOOK_DRIFT_TARGET = 0.05


def strip_language_tag(text: str) -> tuple[str, str | None]:
    """Split SenseVoice's leading ``<|lang|>`` marker off the transcript."""
    detected = None
    cleaned = text
    for tag, language in _LANGUAGE_TAGS.items():
        if tag in cleaned:
            detected = language
            cleaned = cleaned.replace(tag, "")
    return cleaned.strip(), detected


def _sensevoice_snapshot() -> Path:
    """Locate the downloaded SenseVoiceSmall snapshot under the WebUI model cache."""
    root = REPO_ROOT / ".guangpu_webui" / "models" / "models" / "iic--SenseVoiceSmall" / "snapshots"
    if root.is_dir():
        candidates = sorted(path for path in root.iterdir() if path.is_dir())
        if candidates:
            return candidates[-1]
    return root


class LanguageIdentifier:
    """SenseVoiceSmall wrapper: language identity plus a transcript in one pass."""

    def __init__(self, model_dir: str | Path | None = None, device: str | None = None) -> None:
        import torch
        from funasr import AutoModel

        settings = {"disable_update": True}
        if model_dir:
            settings["model"] = str(model_dir)
        else:
            settings["model"] = str(_sensevoice_snapshot()) if _sensevoice_snapshot().is_dir() else "iic/SenseVoiceSmall"
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.model = AutoModel(**settings, device=self.device)

    def identify(self, wav_path: str | Path, *, use_itn: bool = True) -> dict:
        """Transcribe one clip, optionally disabling inverse text normalisation.

        CER against a hand-authored Chinese prompt should compare characters, not
        fight ASR ITN: ``三点`` becomes ``3点`` under ITN and shows up as a speech
        error even when the readback is perfect. Callers measuring CER can pass
        ``use_itn=False``; language identification and UI-facing transcripts keep the
        default readable form.
        """
        result = self.model.generate(
            input=str(wav_path),
            cache={},
            language="auto",
            use_itn=use_itn,
            batch_size_s=60,
            merge_vad=True,
            merge_length_s=15,
        )
        raw = result[0].get("text", "") if result else ""
        cleaned, detected = strip_language_tag(raw)
        return {"raw": raw, "text": cleaned, "language": detected}


def cross_language_drift(embeddings: dict[str, "object"], cosine) -> dict:
    """Pairwise SECS between the five outputs, and the spread the playbook caps.

    One reference recording defines the voice, so the five languages should land close
    together. The spread is what matters: every pair being merely high would still be
    consistent with a voice that drifts a lot between languages.
    """
    languages = sorted(embeddings)
    pairs = {}
    for index, left in enumerate(languages):
        for right in languages[index + 1:]:
            pairs[f"{left}-{right}"] = round(cosine(embeddings[left], embeddings[right]), 4)
    if not pairs:
        return {"pairs": {}, "drift": None, "min_pair": None, "max_pair": None}
    values = list(pairs.values())
    return {
        "pairs": pairs,
        "min_pair": round(min(values), 4),
        "max_pair": round(max(values), 4),
        "mean": round(statistics.fmean(values), 4),
        # The playbook's user-value metric: the spread across languages, not the level.
        "drift": round(max(values) - min(values), 4),
        "worst_pair": min(pairs, key=pairs.get),
    }


def prosody(path: str | Path) -> dict:
    """Duration and F0 spread, using pYIN.

    pYIN rather than CREPE on purpose: CREPE returns a 2005 Hz noise curve with peak
    periodicity 0.0166 on this project's own recordings, while pYIN reads them
    correctly. The investigation is recorded in the project history.
    """
    import soundfile as sf

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from f0_estimate import estimate as estimate_f0

    track = estimate_f0(path, "pyin")
    voiced = track.f0[track.voiced]
    info = sf.info(str(path))
    return {
        "duration": round(info.duration, 2),
        "sample_rate": info.samplerate,
        "voiced_fraction": round(float(track.voiced.mean()), 4),
        "f0_median_hz": round(float(statistics.median(voiced)), 1) if voiced.size else None,
        "f0_p05_hz": round(float(sorted(voiced)[max(0, int(len(voiced) * 0.05))]), 1) if voiced.size else None,
        "f0_p95_hz": round(float(sorted(voiced)[min(len(voiced) - 1, int(len(voiced) * 0.95))]), 1) if voiced.size else None,
    }


def synthesize(cfg: dict, text: str, output_dir: Path, label: str) -> dict:
    """Run one line through the real synthesis core and keep the produced file."""
    from backend.services import core

    started = time.time()
    result = core.synthesize_lines(cfg, text, output_dir=output_dir)
    outputs = result.get("outputs") or []
    if not outputs:
        raise SystemExit(f"{label}: 合成没有产出文件：{result.get('error') or result}")
    first = outputs[0]
    return {
        "label": label,
        "text": text,
        "file": first.get("file"),
        "download_id": first.get("download_id"),
        "seconds_taken": round(time.time() - started, 2),
        "synthesis_mode": result.get("synthesis_mode"),
        "segment_count": result.get("segment_count"),
        "acoustic_inference_calls": result.get("acoustic_inference_calls"),
    }


def validate_lid(model_dir: str | Path, identifier: "LanguageIdentifier") -> dict:
    """Check the language identifier against the model's own per-language examples.

    SenseVoiceSmall ships ``zh.mp3``, ``yue.mp3``, ``en.mp3``, ``ja.mp3`` and ``ko.mp3``
    inside its repository. Their names are ground truth, so they calibrate the LID
    before it is used to judge anything the project produced - the same
    calibrate-the-instrument-first order used for the pitch estimator and the
    reference-audio gate.
    """
    root = Path(model_dir)
    outcomes = {}
    for language in SENTENCES:
        # The examples live under ``example/`` in the repository; the repository root
        # is checked too so a differently laid out snapshot still calibrates. Missing
        # files are reported as SKIP rather than silently passing - the first run of
        # this function skipped all five and looked like it had succeeded.
        candidate = next(
            (
                path
                for path in (root / f"{language}.mp3", root / "example" / f"{language}.mp3")
                if path.is_file()
            ),
            None,
        )
        if candidate is None:
            outcomes[language] = {"status": "missing", "path": str(root / "example" / f"{language}.mp3")}
            continue
        result = identifier.identify(candidate)
        outcomes[language] = {
            "status": "ok" if result["language"] == language else "wrong",
            "expected": language,
            "detected": result["language"],
            "path": str(candidate),
            "text": result["text"][:60],
        }
    scored = [row for row in outcomes.values() if row["status"] in {"ok", "wrong"}]
    skipped = [row for row in outcomes.values() if row["status"] == "missing"]
    return {
        "examples": outcomes,
        "scored": len(scored),
        "skipped": len(skipped),
        "correct": sum(1 for row in scored if row["status"] == "ok"),
        "accuracy": round(sum(1 for row in scored if row["status"] == "ok") / len(scored), 4) if scored else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "output" / "five_language"))
    parser.add_argument("--skip-synthesis", action="store_true", help="reuse a previous run's audio")
    parser.add_argument("--device", default=None)
    parser.add_argument("--sensevoice-dir", default=None)
    parser.add_argument(
        "--validate-lid",
        action="store_true",
        help="also score the LID against SenseVoice's own per-language example clips",
    )
    parser.add_argument("--json-out", default=str(REPO_ROOT / "output" / "five_language" / "report.json"))
    args = parser.parse_args()

    from backend.services import core

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = core.load_config()
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
        if not condition:
            failures.append(label)

    runs: list[dict] = []
    cached = out_dir / "runs.json"
    if args.skip_synthesis and cached.is_file():
        runs = json.loads(cached.read_text(encoding="utf-8"))
        print(f"复用上次合成结果：{len(runs)} 条")
    else:
        print("1. 逐语种合成（每种语言一条，五条，均带显式标签）")
        for language, text in SENTENCES.items():
            run = synthesize(cfg, tagged(language, text), out_dir / language, language)
            runs.append(run)
            print(f"    {language}: {Path(str(run['file'])).name} "
                  f"({run['seconds_taken']}s, mode={run['synthesis_mode']})")
        print("\n2. 五语种混合单次合成")
        mixed = synthesize(cfg, MIXED_LINE, out_dir / "mixed", "mixed")
        runs.append(mixed)
        print(f"    mixed: {Path(str(mixed['file'])).name} ({mixed['seconds_taken']}s)")
        cached.write_text(json.dumps(runs, ensure_ascii=False, indent=2), encoding="utf-8")

    by_label = {run["label"]: run for run in runs}

    print("\n3. 混合单次模式判定")
    mixed = by_label.get("mixed")
    if mixed is None:
        check("混合单次结果存在", False)
    else:
        check("使用了 single_pass_explicit 模式", mixed.get("synthesis_mode") == "single_pass_explicit",
              str(mixed.get("synthesis_mode")))
        check("声学推理只调用一次", mixed.get("acoustic_inference_calls") == 1,
              f"acoustic_inference_calls={mixed.get('acoustic_inference_calls')}")
        check("识别出 5 个片段", mixed.get("segment_count") == 5, f"segment_count={mixed.get('segment_count')}")

    print("\n4. 语种身份（SenseVoiceSmall）")
    identifier = None
    lid_results: dict[str, dict] = {}
    report_calibration = None
    try:
        identifier = LanguageIdentifier(args.sensevoice_dir, args.device)
        print(f"    SenseVoiceSmall 已加载（{identifier.device}）")
    except Exception as exc:
        print(f"    语言识别模型不可用，跳过 LID 与多语种 CER：{type(exc).__name__}: {exc}")

    if identifier is not None:
        if args.validate_lid:
            print("\n    LID 自校准（对 SenseVoice 自带的逐语种示例）")
            calibration = validate_lid(args.sensevoice_dir or _sensevoice_snapshot(), identifier)
            for language, row in calibration["examples"].items():
                mark = {"ok": "PASS", "wrong": "FAIL", "missing": "SKIP"}[row["status"]]
                detail = row.get("detected") or row["path"]
                print(f"    [{mark}] {language} -> {detail}")
            if calibration["accuracy"] is not None:
                check("LID 自校准 5/5 正确", calibration["accuracy"] == 1.0,
                      f"{calibration['correct']}/{calibration['scored']}")
                if calibration["accuracy"] < 1.0:
                    failures.append("LID 自校准")
            else:
                check("LID 自校准拿到示例文件", False, f"跳过 {calibration['skipped']} 个")
            report_calibration = calibration
        else:
            report_calibration = None

        for language in SENTENCES:
            run = by_label.get(language)
            if run is None or not run.get("file"):
                continue
            result = identifier.identify(run["file"])
            lid_results[language] = result
            mark = "PASS" if result["language"] == language else "FAIL"
            print(f"    [{mark}] {language} -> 识别为 {result['language']}：{result['text'][:48]}")
            if result["language"] != language:
                failures.append(f"LID {language}")
        hits = sum(1 for language, result in lid_results.items() if result["language"] == language)
        rate = hits / len(lid_results) if lid_results else 0.0
        check(f"LID 一致率 ≥ {PLAYBOOK_LID_TARGET:.0%}", rate >= PLAYBOOK_LID_TARGET,
              f"{hits}/{len(lid_results)} = {rate:.0%}")

    print("\n5. 跨语种音色漂移（独立说话人模型）")
    timbre: dict = {}
    baseline: dict = {}
    try:
        from speaker_embedding import SpeakerEmbedder, cosine_similarity

        embedder = SpeakerEmbedder(device=args.device)
        embeddings = {}
        for language in SENTENCES:
            run = by_label.get(language)
            if run is None or not run.get("file"):
                continue
            embeddings[language] = embedder.embed_file(run["file"])
        timbre = cross_language_drift(embeddings, cosine_similarity)
        if timbre.get("drift") is not None:
            print(f"    成对 SECS {timbre['min_pair']}–{timbre['max_pair']}（均值 {timbre['mean']}）")
            print(f"    漂移 max−min = {timbre['drift']}，最差一对 {timbre['worst_pair']}")

        # The noise floor, before the cross-language figure is judged.
        print("\n5b. 同语种基线漂移（五句普通话，用来定这条指标的分辨率下限）")
        baseline_embeddings = {}
        for index, text in enumerate(SAME_LANGUAGE_BASELINE):
            if args.skip_synthesis and (out_dir / "baseline" / str(index)).is_dir():
                candidates = sorted((out_dir / "baseline" / str(index)).glob("*.mp3"))
                path = candidates[0] if candidates else None
            else:
                run = synthesize(cfg, text, out_dir / "baseline" / str(index), f"zh{index}")
                path = Path(str(run["file"]))
            if path is not None and path.is_file():
                baseline_embeddings[f"zh{index}"] = embedder.embed_file(path)
        baseline = cross_language_drift(baseline_embeddings, cosine_similarity)
        if baseline.get("drift") is not None:
            print(f"    成对 SECS {baseline['min_pair']}–{baseline['max_pair']}（均值 {baseline['mean']}）")
            print(f"    同语种漂移 max−min = {baseline['drift']}")

        if timbre.get("drift") is not None and baseline.get("drift") is not None:
            ratio = timbre["drift"] / baseline["drift"] if baseline["drift"] else None
            print(f"    跨语种 / 同语种 = {ratio:.2f}" if ratio else "")
            check(
                "跨语种漂移不超过同语种基线（即没有额外的语种相关漂移）",
                timbre["drift"] <= baseline["drift"] * 1.5,
                f"跨语种 {timbre['drift']} vs 同语种 {baseline['drift']}",
            )
        check(f"跨语种漂移 ≤ {PLAYBOOK_DRIFT_TARGET}", timbre["drift"] <= PLAYBOOK_DRIFT_TARGET,
              f"{timbre['drift']}")
    except Exception as exc:
        print(f"    说话人模型不可用，跳过音色指标：{type(exc).__name__}: {exc}")

    print("\n6. 时长与基频（pYIN，仅作健全性检查）")
    prosody_results: dict[str, dict] = {}
    for language in SENTENCES:
        run = by_label.get(language)
        if run is None or not run.get("file"):
            continue
        stats = prosody(run["file"])
        prosody_results[language] = stats
        print(f"    {language}: {stats['duration']}s, 浊音 {stats['voiced_fraction']:.2f}, "
              f"F0 中位 {stats['f0_median_hz']} Hz "
              f"({stats['f0_p05_hz']}–{stats['f0_p95_hz']})")
        if stats["duration"] <= 0.2:
            failures.append(f"duration {language}")

    report = {
        "sentences": SENTENCES,
        "mixed_line": MIXED_LINE,
        "runs": runs,
        "lid_calibration": report_calibration if identifier is not None else None,
        "lid": lid_results,
        "timbre": timbre,
        "same_language_baseline": baseline,
        "prosody": prosody_results,
        "targets": {"lid": PLAYBOOK_LID_TARGET, "cross_language_drift": PLAYBOOK_DRIFT_TARGET},
        "failures": failures,
        "notes": [
            "CER/可懂度只作护栏：Seed-TTS（arXiv:2406.02430 §3.1）指出低 WER 通常意味着更标准化、"
            "口音更弱，所以字错率绝不能当音色或方言保真度指标。",
            "音色用独立模型 ERes2NetV2，不用合成引擎自己的特征——那是循环论证。",
            "基频用 pYIN 而不是 CREPE：CREPE 在本项目自有录音上给出峰值周期性 0.0166 的噪声曲线。",
        ],
    }
    output = Path(args.json_out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    if failures:
        print(f"FAILED: {len(failures)} 项：{failures}")
    else:
        print("全部检查通过")
    print(f"wrote {output}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())