#!/usr/bin/env python3
"""F0 estimation for singing evaluation, with validation against human annotations.

Every singing conclusion this project draws rests on a pitch curve, so the pitch
estimator is itself a measurement instrument that has to be checked before it is
trusted. Vocadito (Bittner, Pasalo, Genchel, Yip & Kim, TISMIR 2022) ships 40 solo
vocal recordings with **hand-annotated F0**, which makes that check possible: the
estimator is run on the audio, compared against the annotations, and only then used.

Two independent estimators are available and both are reported:

* **CREPE** - Kim, Salamon, Li & Bittner, "CREPE: A Convolutional Representation for
  Pitch Estimation", ICASSP 2018 (arXiv:1802.06182). A learned estimator; the
  reference for monophonic vocal pitch in the singing literature.
* **pYIN** - Mauch & Dixon, "pYIN: A Fundamental Frequency Estimator Using
  Probabilistic Threshold Distributions", ICASSP 2014. A classical, deterministic
  estimator that shares no code and no training data with CREPE.

Agreeing on the same curve is evidence; agreeing because they are the same estimator
would not be. Where they disagree the disagreement is reported rather than averaged
away.

F0 error is reported in **cents**, not hertz, because pitch perception is
logarithmic: a 10 Hz error is large at 110 Hz and negligible at 880 Hz. The
conversion is ``1200 * log2(f_estimated / f_reference)``.

Usage:
    .venv\\Scripts\\python.exe scripts\\f0_estimate.py --validate
    .venv\\Scripts\\python.exe scripts\\f0_estimate.py path\\to\\vocal.wav
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]

# CREPE is trained at 16 kHz and its published results are all at that rate, so the
# audio is resampled rather than fed at 44.1 kHz.
CREPE_SAMPLE_RATE = 16000
CREPE_HOP_SAMPLES = 160  # 10 ms, CREPE's usual frame rate

# A frame counts as voiced when the F0 is finite and above this floor. Zero is the
# convention Vocadito uses for "no F0 present".
VOICED_FLOOR_HZ = 1.0

# Search range. C2-C7 covers every singer; Vocadito's annotated range is 107-202 Hz.
DEFAULT_FMIN_HZ = 65.4   # C2
DEFAULT_FMAX_HZ = 2093.0  # C7

# Agreement bands for the validation report. 50 cents is a quarter tone, which is
# the usual "audibly the same note" line; 100 cents is a semitone.
CENTS_CLOSE = 50.0
CENTS_SEMITONE = 100.0

# An error this close to a whole number of octaves is an octave error: the estimator
# picked the right note but the wrong register. This happens at exactly one point -
# the octave - because F0 estimators search over frequency, and a doubling of the
# period is a valid-looking alternative.
OCTAVE_CENTS = 1200.0


@dataclass
class F0Track:
    """One pitch curve on a uniform time grid."""

    times: np.ndarray
    f0: np.ndarray
    voiced: np.ndarray
    method: str
    # Peak confidence the estimator managed. For CREPE this is its periodicity
    # output, and it is the difference between "the singer is silent" and "the model
    # failed": on the user's own slice corpus CREPE returns a median 2005 Hz with a
    # peak periodicity of 0.0166, while pYIN finds a clean 109 Hz in the same audio.
    # Without this field that failure would be reported as a confident pitch curve.
    peak_confidence: float | None = None
    # True when the estimator produced no usable confidence anywhere in the clip.
    degenerate: bool = False

    def resample_to(self, times: np.ndarray) -> np.ndarray:
        """Interpolate this track's F0 onto another time grid.

        Unvoiced frames are carried as 0 and interpolated as such, so the caller
        must compare voiced masks separately rather than trusting the values alone.
        """
        return np.interp(times, self.times, self.f0, left=0.0, right=0.0)

    def reliability(self) -> dict:
        """Whether this curve should be trusted at all, and why not."""
        if self.degenerate:
            return {
                "trustworthy": False,
                "reason": (
                    f"{self.method} 在本段音频上始终没有给出可信的周期性"
                    f"（峰值置信度 {self.peak_confidence}），得到的音高曲线是噪声，不能用于结论。"
                ),
            }
        return {"trustworthy": True, "peak_confidence": self.peak_confidence}


def read_audio(path: str | Path, target_rate: int) -> tuple[np.ndarray, int]:
    """Mono float32 audio, resampled when the file is not already at ``target_rate``."""
    import librosa
    import soundfile as sf

    audio, rate = sf.read(str(path), dtype="float32", always_2d=True)
    mono = audio.mean(axis=1) if audio.shape[1] > 1 else audio[:, 0]
    if rate != target_rate:
        mono = librosa.resample(mono, orig_sr=rate, target_sr=target_rate)
        rate = target_rate
    return np.ascontiguousarray(mono, dtype=np.float32), rate


def estimate_crepe(
    path: str | Path,
    hop_seconds: float = 0.01,
    fmin: float = DEFAULT_FMIN_HZ,
    fmax: float = DEFAULT_FMAX_HZ,
    device: str | None = None,
    batch_size: int = 1024,
    voicing_threshold: float | None = None,
) -> F0Track:
    """Estimate F0 with CREPE at ``hop_seconds`` resolution."""
    f0, confidence, times, rate = crepe_raw(path, hop_seconds, fmin, fmax, device, batch_size)
    threshold = CREPE_VOICING_THRESHOLD if voicing_threshold is None else voicing_threshold
    # CREPE always returns a value; silence shows up as low periodicity and, in
    # practice, as a frequency pinned near fmin. The periodicity threshold is the
    # documented way to get a voicing decision out of it.
    voiced = (confidence >= threshold) & (f0 > VOICED_FLOOR_HZ)
    peak = float(confidence.max()) if confidence.size else 0.0
    return F0Track(
        times=times,
        f0=np.where(voiced, f0, 0.0),
        voiced=voiced,
        method="crepe",
        peak_confidence=round(peak, 4),
        degenerate=peak < DEGENERATE_CONFIDENCE,
    )


def crepe_raw(
    path: str | Path,
    hop_seconds: float = 0.01,
    fmin: float = DEFAULT_FMIN_HZ,
    fmax: float = DEFAULT_FMAX_HZ,
    device: str | None = None,
    batch_size: int = 1024,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """Return ``(f0, periodicity, times, rate)`` before any voicing decision.

    Split out so the voicing threshold can be swept without re-running the network:
    the confidence is the expensive part and the threshold is a free post-processing
    choice, so the calibration below costs one encoder pass instead of one per
    candidate threshold.
    """
    import torch
    import torchcrepe

    audio, rate = read_audio(path, CREPE_SAMPLE_RATE)
    hop = max(1, int(round(hop_seconds * rate)))
    tensor = torch.from_numpy(audio).unsqueeze(0)
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    pitch, periodicity = torchcrepe.predict(
        tensor,
        rate,
        hop_length=hop,
        fmin=fmin,
        fmax=fmax,
        model="full",
        batch_size=batch_size,
        device=device,
        return_periodicity=True,
        pad=True,
    )
    f0 = pitch.squeeze(0).detach().cpu().numpy().astype(np.float64)
    confidence = periodicity.squeeze(0).detach().cpu().numpy().astype(np.float64)
    times = np.arange(f0.size) * (hop / rate)
    return f0, confidence, times, rate


def estimate_pyin(
    path: str | Path,
    hop_seconds: float = 0.01,
    fmin: float = DEFAULT_FMIN_HZ,
    fmax: float = DEFAULT_FMAX_HZ,
) -> F0Track:
    """Estimate F0 with probabilistic YIN (librosa's implementation)."""
    import librosa

    audio, rate = read_audio(path, CREPE_SAMPLE_RATE)
    hop = max(1, int(round(hop_seconds * rate)))
    f0, voiced_flag, _ = librosa.pyin(
        audio, fmin=fmin, fmax=fmax, sr=rate, frame_length=2048, hop_length=hop
    )
    times = np.arange(f0.size) * (hop / rate)
    voiced = np.isfinite(f0) & voiced_flag & (np.nan_to_num(f0) > VOICED_FLOOR_HZ)
    return F0Track(
        times=times,
        f0=np.where(voiced, np.nan_to_num(f0), 0.0),
        voiced=voiced,
        method="pyin",
        peak_confidence=round(float(voiced.mean()), 4),
        degenerate=not bool(voiced.any()),
    )


# CREPE's periodicity output is a confidence in [0, 1]. 0.21 is the value used by
# torchcrepe's own examples for voiced/unvoiced separation; it is stated here rather
# than buried so the validation below can show what it costs. Calibrating it against
# Vocadito annotations found **no better value** - recall is pinned near 0.58 across
# every threshold from 0.02 to 0.60 - so the shipped default is kept and the reason is
# recorded rather than a "tuned" number being adopted from a flat curve.
CREPE_VOICING_THRESHOLD = 0.21

# Below this peak periodicity the estimator found no periodicity anywhere in the
# clip, which means the curve is noise rather than pitch. Measured: real sung audio
# peaks at 0.96, while the user's own slice corpus peaks at 0.0166 - three orders of
# magnitude apart, so the gap is not delicate.
DEGENERATE_CONFIDENCE = 0.05

ESTIMATORS = {"crepe": estimate_crepe, "pyin": estimate_pyin}


def estimate(path: str | Path, method: str = "crepe", **kwargs) -> F0Track:
    return ESTIMATORS[method](path, **kwargs)


# ------------------------------------------------------------------ validation ---


def read_ground_truth(path: str | Path) -> F0Track:
    """Read a Vocadito F0 annotation file: timestamps, then F0 in Hz (0 = unvoiced)."""
    table = np.loadtxt(str(path), delimiter=",", ndmin=2)
    f0 = table[:, 1].astype(np.float64)
    voiced = f0 > VOICED_FLOOR_HZ
    return F0Track(times=table[:, 0], f0=np.where(voiced, f0, 0.0), voiced=voiced, method="ground-truth")


def cents_error(estimated: np.ndarray, reference: np.ndarray) -> np.ndarray:
    """Signed pitch error in cents on frames where both are voiced."""
    ratio = np.clip(estimated, 1e-6, None) / np.clip(reference, 1e-6, None)
    return 1200.0 * np.log2(ratio)


def octave_folded(error_cents: np.ndarray) -> np.ndarray:
    """Fold each error into the nearest octave, so register mistakes stop counting.

    A pitch tracker that reports 220 Hz where the singer sang 110 Hz got the note
    right and the octave wrong. Whether that matters depends on the question, so the
    two are measured separately: raw pitch accuracy keeps the octave error, raw
    chroma accuracy removes it (the standard pair from ``mir_eval``, Raffel et al.,
    ISMIR 2014), and the gap between them *is* the octave-error rate.
    """
    return error_cents - OCTAVE_CENTS * np.round(error_cents / OCTAVE_CENTS)


def compare_tracks(
    estimated: F0Track,
    reference: F0Track,
    tolerance_seconds: float | None = None,
) -> dict:
    """Score one estimated curve against one annotated curve.

    The reference is interpolated onto the estimator's grid rather than the other way
    round: the grid is the estimator's output rate, and resampling a decision the
    estimator already made would hide its own frame-level behaviour.

    A frame is only scored for pitch when **both** agree it is voiced. Counting a
    frame the reference calls silence would reward an estimator for inventing pitch,
    and counting a frame the estimator calls silence would punish it for a decision
    that is scored separately as a voicing error.
    """
    reference_f0 = reference.resample_to(estimated.times)
    # Nearest-sample voicing decision for the reference, on the estimator's grid.
    indices = np.searchsorted(reference.times, estimated.times)
    indices = np.clip(indices, 0, reference.times.size - 1)
    reference_voiced = reference.voiced[indices]

    both = estimated.voiced & reference_voiced
    if tolerance_seconds is not None:
        both &= np.abs(estimated.times - reference.times[indices]) <= tolerance_seconds

    errors = cents_error(estimated.f0[both], reference_f0[both]) if both.any() else np.zeros(0)
    folded = octave_folded(errors) if errors.size else np.zeros(0)

    voiced_true_positive = int(both.sum())
    voiced_false_positive = int((estimated.voiced & ~reference_voiced).sum())
    voiced_false_negative = int((~estimated.voiced & reference_voiced).sum())
    unvoiced_true_negative = int((~estimated.voiced & ~reference_voiced).sum())

    def safe(value: float, digits: int = 3) -> float:
        return round(float(value), digits) if np.isfinite(value) else 0.0

    precision = (
        voiced_true_positive / (voiced_true_positive + voiced_false_positive)
        if voiced_true_positive + voiced_false_positive
        else 0.0
    )
    recall = (
        voiced_true_positive / (voiced_true_positive + voiced_false_negative)
        if voiced_true_positive + voiced_false_negative
        else 0.0
    )

    # Raw pitch accuracy counts every octave mistake as a mistake; raw chroma
    # accuracy forgives the register. Their difference is the octave-error rate, and
    # it is reported explicitly because an RMSE dominated by a handful of octave
    # errors says nothing about how well the tracker follows the melody.
    raw_pitch_accuracy = float(np.mean(np.abs(errors) <= CENTS_CLOSE)) if errors.size else None
    raw_chroma_accuracy = float(np.mean(np.abs(folded) <= CENTS_CLOSE)) if errors.size else None
    octave_error_frames = int(np.sum(
        (np.abs(errors) > CENTS_CLOSE) & (np.abs(folded) <= CENTS_CLOSE)
    )) if errors.size else 0

    return {
        "frames": int(estimated.times.size),
        "scored_frames": int(both.sum()),
        "reliability": estimated.reliability(),
        "voiced_fraction_estimate": safe(estimated.voiced.mean()),
        "voiced_fraction_reference": safe(reference.voiced.mean()),
        "voicing_accuracy": safe((voiced_true_positive + unvoiced_true_negative) / max(estimated.times.size, 1)),
        "voicing_precision": safe(precision),
        "voicing_recall": safe(recall),
        "voiced_false_positive": voiced_false_positive,
        "voiced_false_negative": voiced_false_negative,
        "cents_median_abs": safe(np.median(np.abs(errors)), 2) if errors.size else None,
        "cents_p95_abs": safe(np.percentile(np.abs(errors), 95), 2) if errors.size else None,
        "cents_bias": safe(np.mean(errors), 2) if errors.size else None,
        # RMSE over every scored frame is reported, but read it together with the
        # octave-error count: one octave slip moves RMSE by hundreds of cents while
        # leaving the median untouched.
        "cents_rmse": safe(np.sqrt(np.mean(errors**2)), 2) if errors.size else None,
        "cents_rmse_octave_folded": safe(np.sqrt(np.mean(folded**2)), 2) if folded.size else None,
        "raw_pitch_accuracy": safe(raw_pitch_accuracy) if raw_pitch_accuracy is not None else None,
        "raw_chroma_accuracy": safe(raw_chroma_accuracy) if raw_chroma_accuracy is not None else None,
        "octave_error_frames": octave_error_frames,
        "octave_error_rate": safe(octave_error_frames / max(both.sum(), 1)),
        "within_50_cents": safe(raw_pitch_accuracy) if raw_pitch_accuracy is not None else None,
        "within_100_cents": safe(np.mean(np.abs(errors) <= CENTS_SEMITONE)) if errors.size else None,
    }


def validate(
    dataset_root: str | Path,
    methods: tuple[str, ...] = ("crepe", "pyin"),
    limit: int = 0,
) -> dict:
    """Run every estimator over Vocadito and score it against the annotations."""
    root = Path(dataset_root)
    audio_dir = root / "Audio"
    annotation_dir = root / "Annotations" / "F0"
    clips = sorted(audio_dir.glob("*.wav"))
    if limit:
        clips = clips[:limit]
    if not clips:
        raise SystemExit(f"no clips under {audio_dir}")

    report: dict[str, dict] = {method: {"clips": []} for method in methods}
    for index, clip in enumerate(clips, start=1):
        annotation = annotation_dir / f"{clip.stem}_f0.csv"
        if not annotation.is_file():
            continue
        reference = read_ground_truth(annotation)
        for method in methods:
            try:
                track = estimate(clip, method)
            except Exception as exc:
                report[method]["clips"].append(
                    {"clip": clip.stem, "error": f"{type(exc).__name__}: {exc}"}
                )
                continue
            scores = compare_tracks(track, reference)
            scores["clip"] = clip.stem
            report[method]["clips"].append(scores)
        if index % 10 == 0:
            print(f"  {index}/{len(clips)} clips scored")

    for method in methods:
        scored = [row for row in report[method]["clips"] if "error" not in row]
        report[method]["clips_scored"] = len(scored)
        if not scored:
            continue
        for key in (
            "voicing_accuracy",
            "voicing_precision",
            "voicing_recall",
            "cents_median_abs",
            "cents_p95_abs",
            "cents_rmse",
            "cents_rmse_octave_folded",
            "raw_pitch_accuracy",
            "raw_chroma_accuracy",
            "octave_error_rate",
            "within_100_cents",
        ):
            values = [row[key] for row in scored if row.get(key) is not None]
            if values:
                report[method][key] = {
                    "median": round(float(np.median(values)), 4),
                    "mean": round(float(np.mean(values)), 4),
                    "worst": round(float(max(values)), 4) if key != "voicing_accuracy" else round(float(min(values)), 4),
                }

    # Cross-estimator agreement, computed on the clips both handled.
    if len(methods) >= 2:
        left, right = methods[0], methods[1]
        paired = []
        right_by_clip = {row["clip"]: row for row in report[right]["clips"] if "error" not in row}
        for row in report[left]["clips"]:
            if "error" in row or row["clip"] not in right_by_clip:
                continue
            paired.append(
                {
                    "clip": row["clip"],
                    "cents_median_abs_difference": round(
                        abs((row["cents_median_abs"] or 0) - (right_by_clip[row["clip"]]["cents_median_abs"] or 0)), 2
                    ),
                    "voicing_accuracy_difference": round(
                        abs(row["voicing_accuracy"] - right_by_clip[row["clip"]]["voicing_accuracy"]), 4
                    ),
                }
            )
        report["cross_estimator"] = {
            "method_pair": [left, right],
            "clips_paired": len(paired),
            "median_abs_difference_in_median_error_cents": round(
                float(np.median([row["cents_median_abs_difference"] for row in paired])), 2
            ) if paired else None,
            "median_voicing_accuracy_difference": round(
                float(np.median([row["voicing_accuracy_difference"] for row in paired])), 4
            ) if paired else None,
        }
    return report


def tune_voicing(
    dataset_root: str | Path,
    thresholds: np.ndarray | None = None,
    limit: int = 0,
    device: str | None = None,
) -> dict:
    """Pick CREPE's periodicity threshold from the hand annotations.

    The value shipped in torchcrepe's examples (0.21) turns out to mark only about
    half of the frames the annotators called voiced, so it cannot be adopted as-is. The
    confidence is computed once per clip and every candidate threshold is then scored
    against the annotations for free, which makes a real sweep affordable instead of a
    guess between two values.
    """
    root = Path(dataset_root)
    clips = sorted((root / "Audio").glob("*.wav"))
    if limit:
        clips = clips[:limit]
    thresholds = thresholds if thresholds is not None else np.round(np.arange(0.02, 0.62, 0.02), 3)

    cached: list[tuple[np.ndarray, np.ndarray, np.ndarray, F0Track]] = []
    for index, clip in enumerate(clips, start=1):
        annotation = root / "Annotations" / "F0" / f"{clip.stem}_f0.csv"
        if not annotation.is_file():
            continue
        f0, confidence, times, _ = crepe_raw(clip, device=device)
        cached.append((f0, confidence, times, read_ground_truth(annotation)))
        if index % 10 == 0:
            print(f"  已编码 {index}/{len(clips)}")

    rows = []
    for threshold in thresholds:
        accuracy, precision, recall, f1 = [], [], [], []
        for f0, confidence, times, reference in cached:
            voiced = (confidence >= threshold) & (f0 > VOICED_FLOOR_HZ)
            track = F0Track(times=times, f0=np.where(voiced, f0, 0.0), voiced=voiced, method="crepe")
            scores = compare_tracks(track, reference)
            accuracy.append(scores["voicing_accuracy"])
            precision.append(scores["voicing_precision"])
            recall.append(scores["voicing_recall"])
            denominator = scores["voicing_precision"] + scores["voicing_recall"]
            f1.append(
                2 * scores["voicing_precision"] * scores["voicing_recall"] / denominator
                if denominator
                else 0.0
            )
        rows.append(
            {
                "threshold": float(threshold),
                "accuracy": round(float(np.mean(accuracy)), 4),
                "precision": round(float(np.mean(precision)), 4),
                "recall": round(float(np.mean(recall)), 4),
                "f1": round(float(np.mean(f1)), 4),
            }
        )

    best_accuracy = max(rows, key=lambda row: row["accuracy"])
    best_f1 = max(rows, key=lambda row: row["f1"])
    return {
        "clips": len(cached),
        "sweep": rows,
        "best_accuracy": best_accuracy,
        "best_f1": best_f1,
        "shipped_default": {
            "threshold": CREPE_VOICING_THRESHOLD,
            "row": next((row for row in rows if abs(row["threshold"] - CREPE_VOICING_THRESHOLD) < 1e-9), None),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("audio", nargs="?", help="a file to trace")
    parser.add_argument("--validate", action="store_true", help="validate against Vocadito annotations")
    parser.add_argument("--tune-voicing", action="store_true", help="calibrate CREPE's voicing threshold")
    parser.add_argument("--dataset", default=str(REPO_ROOT / "output" / "svc_eval" / "vocadito"))
    parser.add_argument("--method", default="crepe", choices=sorted(ESTIMATORS))
    parser.add_argument("--methods", default="crepe,pyin", help="estimators to validate, comma separated")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--json-out", default=None)
    parser.add_argument("--device", default=None)
    args = parser.parse_args()

    if args.tune_voicing:
        print("扫 CREPE 的 periodicity 阈值…")
        result = tune_voicing(args.dataset, limit=args.limit, device=args.device)
        print(f"\n{result['clips']} 条素材")
        print(f"{'阈值':>7}{'准确率':>9}{'精确率':>9}{'召回率':>9}{'F1':>9}")
        for row in result["sweep"]:
            print(
                f"{row['threshold']:>7.2f}{row['accuracy']:>9.4f}{row['precision']:>9.4f}"
                f"{row['recall']:>9.4f}{row['f1']:>9.4f}"
            )
        print(f"\n准确率最高：阈值 {result['best_accuracy']['threshold']} "
              f"准确率 {result['best_accuracy']['accuracy']} 召回 {result['best_accuracy']['recall']}")
        print(f"F1 最高    ：阈值 {result['best_f1']['threshold']} F1 {result['best_f1']['f1']}")
        shipped = result["shipped_default"]
        if shipped["row"]:
            print(f"上游示例默认 {shipped['threshold']}: 准确率 {shipped['row']['accuracy']} "
                  f"召回 {shipped['row']['recall']}")
        if args.json_out:
            Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json_out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"wrote {args.json_out}")
        return 0

    if args.validate:
        methods = tuple(item.strip() for item in args.methods.split(",") if item.strip())
        print(f"validating {methods} against Vocadito ground truth…")
        report = validate(args.dataset, methods, args.limit)
        print()
        for method in methods:
            entry = report.get(method, {})
            if not entry.get("clips_scored"):
                print(f"{method}: nothing scored")
                continue
            print(f"{method}: {entry['clips_scored']} clips")
            for key in (
                "voicing_accuracy",
                "voicing_precision",
                "voicing_recall",
                "cents_median_abs",
                "cents_p95_abs",
                "cents_rmse",
                "cents_rmse_octave_folded",
                "raw_pitch_accuracy",
                "raw_chroma_accuracy",
                "octave_error_rate",
                "within_100_cents",
            ):
                stats = entry.get(key)
                if stats:
                    print(f"  {key:<22} median={stats['median']:<10} mean={stats['mean']:<10}")
        cross = report.get("cross_estimator")
        if cross:
            print(
                f"\ncross-estimator: {cross['clips_paired']} paired clips, "
                f"median difference in median error {cross['median_abs_difference_in_median_error_cents']} cents, "
                f"median voicing-accuracy difference {cross['median_voicing_accuracy_difference']}"
            )
        if args.json_out:
            Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"wrote {args.json_out}")
        return 0

    if not args.audio:
        parser.error("give an audio file, or use --validate")
    track = estimate(args.audio, args.method, device=args.device)
    voiced = track.f0[track.voiced]
    print(f"{args.audio} — {track.method}")
    print(f"  frames        : {track.times.size} @ {1 / (track.times[1] - track.times[0]):.1f} Hz")
    print(f"  voiced frames : {track.voiced.sum()} ({track.voiced.mean() * 100:.1f}%)")
    if voiced.size:
        print(f"  F0 range      : {voiced.min():.1f} – {voiced.max():.1f} Hz "
              f"(median {np.median(voiced):.1f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())