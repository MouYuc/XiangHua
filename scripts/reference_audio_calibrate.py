"""Calibrate the reference-audio gate against known-good and known-bad audio.

Populations:
  A. the vendor's 18 official DiaMoE prompts  -> must PASS (a gate that rejects the
     shipped prompts is miscalibrated by definition)
  B. the user's 64 slice corpus               -> the timbre purpose population
  C. A resampled down to 8 kHz and back up     -> ground-truth NARROWBAND negative
  D. A with white noise at a known SNR         -> ground-truth NOISE negative
  E. A with gain into hard clipping            -> ground-truth CLIP negative
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import soundfile as sf

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from backend.services import reference_audio as ra  # noqa: E402

PROMPTS = sorted((REPO_ROOT / "third_party" / "DiaMoE-TTS" / "prompts").glob("*.wav"))
SLICES = sorted(p for p in (REPO_ROOT / "GPT-SoVITS" / "custom_data" / "sliced").iterdir()
                if p.suffix.lower() in {".wav", ".mp3", ".flac"})
WORK = REPO_ROOT / "output" / "ref_gate_calibration"
WORK.mkdir(parents=True, exist_ok=True)


def resample(audio: np.ndarray, src: int, dst: int) -> np.ndarray:
    import librosa
    return librosa.resample(audio, orig_sr=src, target_sr=dst)


def make_narrowband(path: Path) -> Path:
    audio, rate = ra.load_mono(path)
    up = resample(resample(audio, rate, 8000), 8000, rate)
    out = WORK / f"narrowband_{path.stem}.wav"
    sf.write(str(out), up, rate)
    return out


def make_noisy(path: Path, target_snr_db: float) -> Path:
    """Add white noise at a stated SNR relative to the speech level (70th pct)."""
    audio, rate = ra.load_mono(path)
    rms = ra.frame_rms(audio, rate)
    speech = float(np.percentile(rms, ra.SPEECH_PERCENTILE))
    noise_rms = speech / (10.0 ** (target_snr_db / 20.0))
    rng = np.random.default_rng(0)
    noise = rng.normal(0, noise_rms, audio.size).astype(np.float32)
    out = WORK / f"noisy{int(target_snr_db)}_{path.stem}.wav"
    sf.write(str(out), np.clip(audio + noise, -1, 1), rate)
    return out


def make_clipped(path: Path) -> Path:
    audio, rate = ra.load_mono(path)
    out = WORK / f"clipped_{path.stem}.wav"
    sf.write(str(out), np.clip(audio * 6.0, -1.0, 1.0), rate)
    return out


def summarize(paths: list[Path], label: str, purpose: str) -> list[dict]:
    reports = []
    for path in paths:
        try:
            reports.append(ra.analyze(path, purpose, with_f0=False))
        except Exception as exc:
            print(f"  !! {path.name}: {type(exc).__name__}: {exc}")
    if not reports:
        return []
    dur = np.array([r.duration_seconds for r in reports])
    high = np.array([r.metrics["high_band_db"] if r.metrics["high_band_db"] is not None else np.nan for r in reports])
    snr = np.array([r.metrics["estimated_snr_db"] if r.metrics["estimated_snr_db"] is not None else np.nan for r in reports])
    sil = np.array([r.metrics["silence_ratio"] for r in reports])
    clip = np.array([r.metrics["clipping_ratio"] for r in reports])
    peak = np.array([r.metrics["peak_dbfs"] for r in reports])
    print(f"\n=== {label} (n={len(reports)}, purpose={purpose})")
    for name, arr, fmt in (
        ("duration s", dur, "{:.2f}"),
        ("high_band dB", high, "{:.1f}"),
        ("snr dB", snr, "{:.1f}"),
        ("silence", sil, "{:.3f}"),
        ("clipping", clip, "{:.5f}"),
        ("peak dBFS", peak, "{:.1f}"),
    ):
        if np.all(np.isnan(arr)):
            print(f"  {name:<14} all NaN")
            continue
        print(
            f"  {name:<14} min={fmt.format(np.nanmin(arr)):>9}"
            f"  p05={fmt.format(np.nanpercentile(arr, 5)):>9}"
            f"  med={fmt.format(np.nanmedian(arr)):>9}"
            f"  p95={fmt.format(np.nanpercentile(arr, 95)):>9}"
            f"  max={fmt.format(np.nanmax(arr)):>9}   nan={int(np.isnan(arr).sum())}"
        )
    counts = {"ok": 0, "warn": 0, "fail": 0}
    for report in reports:
        counts[report.status] += 1
    print(f"  verdicts: {counts['ok']} ok / {counts['warn']} warn / {counts['fail']} fail")
    return [r.to_dict() for r in reports]


def main() -> int:
    sample = PROMPTS[:6]
    narrow = [make_narrowband(p) for p in sample]
    clipped = [make_clipped(p) for p in sample]
    noise_levels = [30.0, 20.0, 15.0, 10.0, 5.0]

    results = {}
    results["vendor_prompts"] = summarize(PROMPTS, "A. vendor official prompts", "synthesis")
    results["user_slices"] = summarize(SLICES, "B. user slice corpus (per file)", "timbre")
    results["narrowband"] = summarize(narrow, "C. GROUND TRUTH narrowband (8 kHz round-trip)", "synthesis")
    results["clipped"] = summarize(clipped, "E. GROUND TRUTH clipped (x6 gain)", "synthesis")
    for level in noise_levels:
        noisy = [make_noisy(p, level) for p in sample]
        results[f"noise_{level:g}db"] = summarize(
            noisy, f"D. GROUND TRUTH white noise injected at {level:g} dB SNR", "synthesis"
        )

    print("\n--- monotonicity check: injected SNR vs measured SNR ---")
    print(f"{'injected':>10}{'measured median':>18}{'status':>8}")
    for level in noise_levels:
        rows = results[f"noise_{level:g}db"]
        measured = [r["metrics"]["estimated_snr_db"] for r in rows if r["metrics"]["estimated_snr_db"] is not None]
        statuses = [r["status"] for r in rows]
        median = f"{np.median(measured):.1f}" if measured else "n/a"
        verdict = max(set(statuses), key=statuses.count) if statuses else "?"
        print(f"{level:>10.0f}{median:>18}{verdict:>8}")

    print("\n--- bandwidth separation (same 6 files) ---")
    for original, narrowband in zip(sample, narrow):
        a = ra.analyze(original, "synthesis")
        b = ra.analyze(narrowband, "synthesis")
        print(f"  {original.name[:32]:<34} wideband={a.metrics['high_band_db']:>8} "
              f"narrowband={b.metrics['high_band_db']:>8}  gap="
              f"{abs(a.metrics['high_band_db'] - b.metrics['high_band_db']):>7.1f} dB")

    import json
    (WORK / "calibration.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nwrote {WORK / 'calibration.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())