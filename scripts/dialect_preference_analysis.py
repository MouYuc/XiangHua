#!/usr/bin/env python3
"""Join human listening picks with the objective metrics of the candidates.

The point of this tool is to answer one question honestly: does any metric we can
compute actually predict which dialect candidate a human prefers? If not, the
automatic selector must not pretend it can choose.

Coordinate system of the analysis:

* one row per (dialect, probe, seed) candidate rendered by ``dialect_baseline.py``;
* objective columns from the run manifest plus acoustic features measured here;
* one human label per (dialect, probe) group, stored in
  ``assets/dialect_listening_labels.json``.

For every feature the human pick is ranked inside its own group, so features that
carry no information average a rank of 1.5 on a 0..3 scale.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import find_peaks

DEFAULT_LABELS = Path("assets/dialect_listening_labels.json")
DEFAULT_RUN = Path("output/dialect_baseline/20260923_224346")


def envelope(audio: np.ndarray, rate: int, frame: int = 240, hop: int = 80) -> np.ndarray:
    frames = 1 + (len(audio) - frame) // hop
    rms = np.array([np.sqrt(np.mean(audio[i * hop:i * hop + frame] ** 2)) for i in range(frames)])
    return np.convolve(rms, np.ones(3) / 3.0, mode="same")


def acoustic_features(path: Path) -> dict:
    """Features that plausibly separate clear, natural speech from mumbled or chopped speech."""
    audio, rate = sf.read(str(path), always_2d=True)
    audio = audio.mean(axis=1).astype(np.float64)
    audio = audio / (np.abs(audio).max() + 1e-9)

    env = envelope(audio, rate)
    env_rate = rate / 80
    floor = float(np.percentile(env, 5))
    top = float(env.max())
    span = max(top - floor, 1e-9)

    # Syllable-rate modulation: how much energy sits in the 2-8 Hz band, which is
    # where syllable rhythm lives. Mumbled compression flattens this band.
    spectrum = np.abs(np.fft.rfft(env - env.mean()))
    freqs = np.fft.rfftfreq(len(env), d=1.0 / env_rate)
    band = (freqs >= 2.0) & (freqs <= 8.0)
    modulation_ratio = float(spectrum[band].sum() / (spectrum[1:].sum() + 1e-12))

    peaks, _ = find_peaks(env, distance=int(0.085 * env_rate),
                          prominence=0.12 * span, height=floor + 0.10 * span)
    if len(peaks) > 2:
        intervals = np.diff(peaks) / env_rate
        nucleus_cv = float(np.std(intervals) / (np.mean(intervals) + 1e-9))
    else:
        nucleus_cv = 1.0

    # Voiced-frame pitch spread: a monotone or jumpy contour reads as unnatural.
    try:
        import librosa

        f0 = librosa.yin(audio, fmin=60, fmax=400, sr=rate, frame_length=1024, hop_length=80)
        voiced = f0[(f0 > 70) & (f0 < 350)]
        f0_log_std = float(np.std(np.log(voiced))) if len(voiced) > 5 else 0.0
        flatness = float(np.mean(librosa.feature.spectral_flatness(y=audio, hop_length=80)))
        zcr = float(np.mean(librosa.feature.zero_crossing_rate(audio, hop_length=80)))
    except Exception:
        f0_log_std = 0.0
        flatness = 0.0
        zcr = 0.0

    voiced_frames = env > (floor + 0.06 * span)
    return {
        "modulation_ratio": round(modulation_ratio, 4),
        "nucleus_cv": round(nucleus_cv, 4),
        "f0_log_std": round(f0_log_std, 4),
        "spectral_flatness": round(float(flatness), 5),
        "zero_crossing_rate": round(float(zcr), 5),
        "voiced_ratio": round(float(voiced_frames.mean()), 4),
        "energy_cv": round(float(np.std(env) / (np.mean(env) + 1e-9)), 4),
    }


# Which direction of each feature should sound better, if the feature matters at all.
HIGHER_IS_BETTER = {
    "modulation_ratio": True,
    "nucleus_cv": False,
    "f0_log_std": True,
    "spectral_flatness": False,
    "zero_crossing_rate": False,
    "voiced_ratio": True,
    "energy_cv": True,
    "cer": None,          # deliberately unsigned: the question is whether it predicts anything
    "nuclei": True,
    "longest_gap": False,
    "trailing_silence": False,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", default=str(DEFAULT_LABELS))
    parser.add_argument("--run-dir", default=str(DEFAULT_RUN))
    parser.add_argument("--skip-acoustic", action="store_true")
    args = parser.parse_args()

    labels = json.loads(Path(args.labels).read_text(encoding="utf-8"))
    run_dir = Path(args.run_dir)
    results = json.loads((run_dir / "baseline.json").read_text(encoding="utf-8"))
    preferred = {(item["dialect"], item["probe"]): item["preferred_seed"] for item in labels["labels"]}

    groups = results["groups"]["user_reference"] + results["groups"]["frontend_only"]
    rows = []
    for entry in groups:
        for record in entry["records"]:
            if "candidates" not in record:
                continue
            key = (entry["dialect"], record["probe"])
            if key not in preferred:
                continue
            for candidate in record["candidates"]:
                row = {
                    "dialect": entry["dialect"],
                    "probe": record["probe"],
                    "seed": candidate["seed"],
                    "auto_pick": candidate["seed"] == record["seed"],
                    "human_pick": candidate["seed"] == preferred[key],
                    "wav": candidate["wav"],
                    "duration": candidate["duration"],
                    "syllables": candidate["syllables"],
                    "cer": candidate["cer"],
                    "nuclei": candidate["nuclei"],
                    "longest_gap": candidate["longest_gap"],
                    "trailing_silence": candidate["trailing_silence"],
                }
                rows.append(row)

    if not args.skip_acoustic:
        cache: dict[str, dict] = {}
        for row in rows:
            if row["wav"] not in cache:
                cache[row["wav"]] = acoustic_features(Path(row["wav"]))
            row.update(cache[row["wav"]])

    # Group-level ranking of the human pick on every feature.
    features = [key for key in HIGHER_IS_BETTER if key in rows[0]]
    group_keys = sorted({(row["dialect"], row["probe"]) for row in rows})
    rank_summary = {}
    for feature in features:
        ranks = []
        for dialect, probe in group_keys:
            members = [row for row in rows if row["dialect"] == dialect and row["probe"] == probe]
            values = [row[feature] for row in members]
            order = sorted(range(len(members)), key=lambda i: values[i])
            rank_of = {order[i]: i for i in range(len(order))}
            for index, row in enumerate(members):
                if row["human_pick"]:
                    ranks.append(rank_of[index])
        if ranks:
            rank_summary[feature] = {
                "mean_rank": round(float(np.mean(ranks)), 3),
                "top_rank_rate": round(sum(1 for r in ranks if r == 3) / len(ranks), 3),
                "bottom_rank_rate": round(sum(1 for r in ranks if r == 0) / len(ranks), 3),
                "groups": len(ranks),
            }

    auto_hits = sum(1 for row in rows if row["auto_pick"] and row["human_pick"])
    groups_count = len(group_keys)
    seed_hist = Counter(item["preferred_seed"] for item in labels["labels"])

    report = {
        "run_dir": str(run_dir),
        "groups": groups_count,
        "candidates": len(rows),
        "auto_pick_agreement": auto_hits,
        "auto_pick_agreement_rate": round(auto_hits / groups_count, 3),
        "chance_agreement_rate": round(1.0 / (len(rows) / groups_count), 3),
        "preferred_seed_histogram": {str(k): v for k, v in sorted(seed_hist.items())},
        "feature_rank_summary": rank_summary,
        "rows": rows,
    }
    (run_dir / "preference.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())