#!/usr/bin/env python3
"""Objective metrics for singing voice conversion (SVC).

Two questions have to be answered separately, because a converter can succeed at one
while failing the other:

1. **Did the melody survive?** A singing converter must move the timbre without
   moving the notes. Measured by comparing the converted pitch curve against the
   **source** curve: error in cents, correlation of log-F0, and the voiced/unvoiced
   decision error rate.
2. **Did the timbre change to the target?** Measured by speaker embedding cosine
   similarity (SECS) from a model that is independent of the converter.

Both halves are needed. A converter that reproduces the input exactly scores
perfectly on (1) and fails (2); one that produces target-sounding noise can score well
on (2) and fails (1).

Why the pitch numbers are reported as a set rather than one number
------------------------------------------------------------------

A single RMSE figure is dominated by octave errors - a handful of frames where the
tracker picked the right note in the wrong register move RMSE by hundreds of cents
while leaving the median untouched. Folded RMSE, raw pitch accuracy, raw chroma
accuracy and the octave-error rate are therefore reported together
(``mir_eval`` conventions, Raffel et al., ISMIR 2014), and the estimator itself is
validated against hand annotations first - see ``scripts/f0_estimate.py``.

A caveat that must travel with every number here
------------------------------------------------

**The kNN-VC paper contains no singing experiments.** Baas, van Niekerk & Kamper,
"kNN-VC: Non-parametric voice conversion with k-nearest-neighbor regression",
Interspeech 2023 (arXiv:2305.18975), evaluates speech only. There is therefore no
published baseline this project can compare against, and none of the thresholds below
are quoted from a paper - they are stated as project targets and must be read as such.

Usage:
    .venv\\Scripts\\python.exe scripts\\svc_metrics.py --source a.wav --converted b.wav
    .venv\\Scripts\\python.exe scripts\\svc_metrics.py --reference ref.wav --source a.wav --converted b.wav
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

from f0_estimate import (  # noqa: E402
    CENTS_CLOSE,
    OCTAVE_CENTS,
    F0Track,
    cents_error,
    compare_tracks,
    estimate,
    octave_folded,
)

# Project targets, not paper thresholds. See the module docstring: the kNN-VC paper
# has no singing experiments, so there is no published number to adopt. These are
# stated so a regression is visible, and they are recorded as project targets in
# ``docs/SVC_SINGING_EVAL.md`` rather than presented as literature values.
PROJECT_TARGETS = {
    "f0_median_abs_cents": 50.0,      # within a quarter tone of the source melody
    "f0_chroma_accuracy": 0.90,       # 90% of voiced frames on the right note
    "vuv_error_rate": 0.15,           # voiced/unvoiced decisions mostly preserved
    "f0_correlation": 0.80,           # the contour shape, not just the level
    "secs_to_target": 0.70,           # target timbre clearly adopted
    "secs_margin_over_source": 0.15,  # and clearly better than leaving it alone
}


def log_f0_correlation(source: F0Track, converted: F0Track) -> dict:
    """Pearson correlation of log-F0 over jointly voiced frames.

    Correlation is computed on **log** F0 so that the measure is in cents rather than
    hertz; otherwise the high notes dominate simply because hertz are bigger up there.
    The number answers "does the converted contour have the same shape", which an RMSE
    cannot: a constant transposition has a large RMSE and a correlation of 1.
    """
    grid = converted.times
    source_f0 = source.resample_to(grid)
    indices = np.clip(np.searchsorted(source.times, grid), 0, source.times.size - 1)
    source_voiced = source.voiced[indices]
    both = converted.voiced & source_voiced
    if both.sum() < 2:
        return {"frames": int(both.sum()), "correlation": None}

    left = np.log2(np.clip(source_f0[both], 1e-6, None))
    right = np.log2(np.clip(converted.f0[both], 1e-6, None))
    if np.std(left) == 0 or np.std(right) == 0:
        # A perfectly steady note has no contour to correlate; reported as
        # unmeasurable rather than as zero, which would read as "no agreement".
        return {"frames": int(both.sum()), "correlation": None, "note": "音高几乎不变，相关系数无从计算"}
    return {
        "frames": int(both.sum()),
        "correlation": round(float(np.corrcoef(left, right)[0, 1]), 4),
        # A constant transposition leaves correlation at 1 but shows up as a bias.
        "median_transposition_cents": round(
            float(np.median(cents_error(converted.f0[both], source_f0[both]))), 2
        ),
    }


def voiced_decision_error(source: F0Track, converted: F0Track) -> dict:
    """How far the converted voicing decisions moved from the source's."""
    grid = converted.times
    indices = np.clip(np.searchsorted(source.times, grid), 0, source.times.size - 1)
    source_voiced = source.voiced[indices]
    if grid.size == 0:
        return {"frames": 0, "error_rate": None}
    disagree = int((converted.voiced != source_voiced).sum())
    return {
        "frames": int(grid.size),
        "disagreeing_frames": disagree,
        "error_rate": round(disagree / grid.size, 4),
        "source_voiced_fraction": round(float(source_voiced.mean()), 4),
        "converted_voiced_fraction": round(float(converted.voiced.mean()), 4),
    }


def pitch_preservation(source: F0Track, converted: F0Track) -> dict:
    """Compare a converted pitch curve against the source curve it came from.

    ``compare_tracks`` was written to score an estimator against an annotation, and
    the roles line up exactly: the source is the reference the converted clip is
    supposed to follow.

    A degenerate track short-circuits the whole comparison. Reporting "median error
    1126 cents" from a curve whose estimator found no periodicity at all would be
    worse than reporting nothing, because the number looks like a measurement.
    """
    unreliable = [label for label, track in (("源音频", source), ("转换结果", converted)) if track.degenerate]
    if unreliable:
        return {
            "trustworthy": False,
            "reason": f"{'、'.join(unreliable)}的音高曲线不可信，本次对比作废。",
            "source_reliability": source.reliability(),
            "converted_reliability": converted.reliability(),
        }

    scores = compare_tracks(converted, source)
    correlation = log_f0_correlation(source, converted)
    voicing = voiced_decision_error(source, converted)
    return {
        "trustworthy": True,
        "median_abs_cents": scores["cents_median_abs"],
        "p95_abs_cents": scores["cents_p95_abs"],
        "bias_cents": scores["cents_bias"],
        "rmse_cents": scores["cents_rmse"],
        "rmse_cents_octave_folded": scores["cents_rmse_octave_folded"],
        "raw_pitch_accuracy": scores["raw_pitch_accuracy"],
        "raw_chroma_accuracy": scores["raw_chroma_accuracy"],
        "octave_error_rate": scores["octave_error_rate"],
        "correlation": correlation["correlation"],
        "median_transposition_cents": correlation.get("median_transposition_cents"),
        "vuv_error_rate": voicing["error_rate"],
        "scored_frames": scores["scored_frames"],
        "source_voiced_fraction": voicing["source_voiced_fraction"],
        "converted_voiced_fraction": voicing["converted_voiced_fraction"],
    }


def timbre_similarity(
    reference: str | Path | None,
    source: str | Path,
    converted: str | Path,
    embedder=None,
) -> dict:
    """Independent SECS: converted-vs-target, and converted-vs-source for contrast.

    The second number is what makes the first interpretable. A converter that leaves
    the input untouched scores highly against the source and poorly against the
    target; without that baseline, "SECS 0.72 to target" is a number with no scale.

    The embedder is required to be independent of the conversion itself - see
    ``scripts/speaker_embedding.py`` for why the WavLM features used by kNN-VC cannot
    serve as their own evidence.
    """
    from speaker_embedding import SpeakerEmbedder, cosine_similarity

    embedder = embedder or SpeakerEmbedder()
    source_embedding = embedder.embed_file(source)
    converted_embedding = embedder.embed_file(converted)
    result = {
        "secs_converted_vs_source": round(cosine_similarity(converted_embedding, source_embedding), 4),
    }
    if reference is not None:
        reference_embedding = embedder.embed_file(reference)
        result["secs_converted_vs_target"] = round(
            cosine_similarity(converted_embedding, reference_embedding), 4
        )
        result["secs_source_vs_target"] = round(
            cosine_similarity(source_embedding, reference_embedding), 4
        )
        # Positive means conversion moved the voice towards the target rather than
        # away from it; this single number is the actual claim being tested.
        result["secs_improvement"] = round(
            result["secs_converted_vs_target"] - result["secs_source_vs_target"], 4
        )
    return result


def cross_check_estimators(
    source: str | Path,
    converted: str | Path,
    primary: str = "crepe",
    secondary: str = "pyin",
    device: str | None = None,
) -> dict:
    """Run the pitch comparison with a second, unrelated estimator.

    The two share no code and no training data, so agreement is evidence and
    disagreement is a warning. This matters here specifically: CREPE is validated on
    Vocadito (median error 8.6 cents, raw pitch accuracy 0.982) but returns a median
    2005 Hz with peak periodicity 0.0166 on the user's own reference recordings, where
    pYIN reads a clean 109 Hz. A single-estimator conclusion would have inherited that
    blind spot silently.
    """
    results = {}
    for method in (primary, secondary):
        try:
            source_track = estimate(source, method, device=device) if method == "crepe" else estimate(source, method)
            converted_track = (
                estimate(converted, method, device=device) if method == "crepe" else estimate(converted, method)
            )
            results[method] = pitch_preservation(source_track, converted_track)
        except Exception as exc:
            results[method] = {"trustworthy": False, "reason": f"{type(exc).__name__}: {exc}"}

    usable = {key: value for key, value in results.items() if value.get("trustworthy")}
    agreement = None
    if len(usable) == 2:
        left, right = usable[primary], usable[secondary]
        if left.get("median_abs_cents") is not None and right.get("median_abs_cents") is not None:
            agreement = {
                "median_abs_cents_difference": round(
                    abs(left["median_abs_cents"] - right["median_abs_cents"]), 2
                ),
                "correlation_difference": round(abs((left.get("correlation") or 0) - (right.get("correlation") or 0)), 4),
            }
    return {"estimators": results, "agreement": agreement, "agreeing_estimators": sorted(usable)}


def evaluate(
    source: str | Path,
    converted: str | Path,
    reference: str | Path | None = None,
    method: str = "crepe",
    device: str | None = None,
    with_timbre: bool = True,
) -> dict:
    """Full objective report for one converted clip."""
    source_track = estimate(source, method, device=device) if method == "crepe" else estimate(source, method)
    converted_track = (
        estimate(converted, method, device=device) if method == "crepe" else estimate(converted, method)
    )
    report = {
        "source": str(source),
        "converted": str(converted),
        "reference": str(reference) if reference else None,
        "f0_method": method,
        "source_f0": {
            "frames": int(source_track.times.size),
            "voiced_fraction": round(float(source_track.voiced.mean()), 4),
        },
        "pitch": pitch_preservation(source_track, converted_track),
    }
    if with_timbre:
        try:
            report["timbre"] = timbre_similarity(reference, source, converted)
        except FileNotFoundError as exc:
            report["timbre"] = {"error": str(exc)}
    report["targets"] = check_targets(report)
    return report


def check_targets(report: dict) -> dict:
    """Compare the report against the project targets, keeping the failures visible."""
    pitch = report.get("pitch", {})
    timbre = report.get("timbre", {})
    checks = {
        "f0_median_abs_cents": (
            pitch.get("median_abs_cents") is not None
            and pitch["median_abs_cents"] <= PROJECT_TARGETS["f0_median_abs_cents"]
        ),
        "f0_chroma_accuracy": (
            pitch.get("raw_chroma_accuracy") is not None
            and pitch["raw_chroma_accuracy"] >= PROJECT_TARGETS["f0_chroma_accuracy"]
        ),
        "vuv_error_rate": (
            pitch.get("vuv_error_rate") is not None
            and pitch["vuv_error_rate"] <= PROJECT_TARGETS["vuv_error_rate"]
        ),
        "f0_correlation": (
            pitch.get("correlation") is not None
            and pitch["correlation"] >= PROJECT_TARGETS["f0_correlation"]
        ),
    }
    if "secs_converted_vs_target" in timbre:
        checks["secs_to_target"] = timbre["secs_converted_vs_target"] >= PROJECT_TARGETS["secs_to_target"]
        checks["secs_margin_over_source"] = (
            timbre.get("secs_improvement", 0.0) >= PROJECT_TARGETS["secs_margin_over_source"]
        )
    return {
        "passed": {key: value for key, value in checks.items() if value},
        "failed": [key for key, value in checks.items() if not value],
        "thresholds": PROJECT_TARGETS,
        "note": "这些阈值是本项目的目标值，不是论文数值：kNN-VC 论文没有歌声实验，没有可引用的基线。",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True, help="the vocal that was converted (the melody to preserve)")
    parser.add_argument("--converted", required=True, help="the converted output")
    parser.add_argument("--reference", default=None, help="the target timbre recording")
    parser.add_argument("--method", default="crepe", choices=("crepe", "pyin"))
    parser.add_argument("--device", default=None)
    parser.add_argument("--no-timbre", action="store_true", help="skip the speaker-embedding half")
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args()

    report = evaluate(
        args.source, args.converted, args.reference,
        method=args.method, device=args.device, with_timbre=not args.no_timbre,
    )
    pitch = report["pitch"]
    print(f"source    : {Path(args.source).name}")
    print(f"converted : {Path(args.converted).name}")
    print(f"f0 method : {args.method}")
    print("\n旋律保持（对比源音频）")
    print(f"  中位误差          {pitch['median_abs_cents']} 音分")
    print(f"  p95 误差          {pitch['p95_abs_cents']} 音分")
    print(f"  偏置              {pitch['bias_cents']} 音分")
    print(f"  RMSE              {pitch['rmse_cents']} 音分（折叠八度后 {pitch['rmse_cents_octave_folded']}）")
    print(f"  原调准确率 RPA    {pitch['raw_pitch_accuracy']}")
    print(f"  音级准确率 RCA    {pitch['raw_chroma_accuracy']}")
    print(f"  八度错误率        {pitch['octave_error_rate']}")
    print(f"  对数 F0 相关系数  {pitch['correlation']}")
    print(f"  清浊判定错误率    {pitch['vuv_error_rate']}")
    if "timbre" in report:
        timbre = report["timbre"]
        print("\n音色（独立说话人验证模型）")
        if "error" in timbre:
            print(f"  跳过：{timbre['error']}")
        else:
            print(f"  转换结果 vs 目标  {timbre.get('secs_converted_vs_target')}")
            print(f"  源音频   vs 目标  {timbre.get('secs_source_vs_target')}")
            print(f"  提升              {timbre.get('secs_improvement')}")
            print(f"  转换结果 vs 源    {timbre['secs_converted_vs_source']}")
    targets = report["targets"]
    print(f"\n目标核对：通过 {len(targets['passed'])} 项，未达 {targets['failed'] or '无'}")
    print(f"注：{targets['note']}")
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())