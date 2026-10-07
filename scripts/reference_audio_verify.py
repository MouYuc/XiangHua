#!/usr/bin/env python3
"""Acceptance verification for the reference-audio quality gate.

A gate is only trustworthy if it is checked against audio whose defect is known in
advance. This script builds those populations from in-repo audio, runs the gate, and
asserts the verdict for each. It is the evidence behind the thresholds in
``backend/services/reference_audio.py`` and the numbers in
``docs/REFERENCE_AUDIO_GATE.md``.

Known-good populations that must NOT be rejected:
  * the vendor's own 18 official DiaMoE prompts, judged as synthesis prompts
  * the user's 64-slice corpus, judged as a whole corpus (the unit kNN-VC uses)

Known-bad populations, each a deliberate copy of the above:
  * narrowband: resampled to 8 kHz and back up, so the sample rate lies
  * clipped: gain into hard clipping
  * noisy: white noise injected at a stated SNR, measured as a corpus

Usage:
    .venv\\Scripts\\python.exe scripts\\reference_audio_verify.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.services import reference_audio as ra  # noqa: E402

PROMPTS = sorted((REPO_ROOT / "third_party" / "DiaMoE-TTS" / "prompts").glob("*.wav"))
SLICES = sorted(
    path for path in (REPO_ROOT / "GPT-SoVITS" / "custom_data" / "sliced").iterdir()
    if path.suffix.lower() in {".wav", ".mp3", ".flac"}
)
WORK = REPO_ROOT / "output" / "ref_gate_verify"
WORK.mkdir(parents=True, exist_ok=True)

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        FAILURES.append(label)


def resample(audio: np.ndarray, src: int, dst: int) -> np.ndarray:
    import librosa

    return librosa.resample(audio, orig_sr=src, target_sr=dst)


def make_narrowband(source: Path, destination: Path) -> Path:
    audio, rate = ra.load_mono(source)
    upsampled = resample(resample(audio, rate, 8000), 8000, rate)
    sf.write(str(destination), upsampled, rate)
    return destination


def make_clipped(source: Path, destination: Path) -> Path:
    """Normalise to full scale first, then drive it well past it.

    Scaling a quiet recording by a fixed factor does not clip it, so the first
    version of this negative produced files that were quietly untouched and the gate
    was wrongly credited with catching them.
    """
    audio, rate = ra.load_mono(source)
    peak = float(np.abs(audio).max()) or 1.0
    sf.write(str(destination), np.clip(audio / peak * 3.0, -1.0, 1.0), rate)
    return destination


def make_noisy(source: Path, destination: Path, snr_db: float) -> Path:
    audio, rate = ra.load_mono(source)
    rms = ra.frame_rms(audio, rate)
    speech = float(np.percentile(rms, ra.SPEECH_PERCENTILE))
    rng = np.random.default_rng(0)
    noise = rng.normal(0, speech / (10.0 ** (snr_db / 20.0)), audio.size).astype(np.float32)
    sf.write(str(destination), np.clip(audio + noise, -1, 1), rate)
    return destination


def concat(paths: list[Path], destination: Path) -> Path:
    """Concatenate a corpus into one file, as the aggregate the gate judges."""
    import librosa

    pieces = []
    target = 22050
    for path in paths:
        audio, rate = ra.load_mono(path)
        pieces.append(librosa.resample(audio, orig_sr=rate, target_sr=target))
    sf.write(str(destination), np.concatenate(pieces), target)
    return destination


def main() -> int:
    print("1. Vendor's own 18 official prompts must not be rejected (purpose=synthesis)")
    prompt_reports = [ra.analyze(path, "synthesis") for path in PROMPTS]
    verdicts = {"ok": 0, "warn": 0, "fail": 0}
    for report in prompt_reports:
        verdicts[report.status] += 1
    print(f"    {verdicts['ok']} ok / {verdicts['warn']} warn / {verdicts['fail']} fail")
    failing = [r for r in prompt_reports if r.status == "fail"]
    for report in failing:
        keys = [c.key for c in report.checks if c.status == "fail"]
        print(f"    fail: {Path(report.path).name} {keys}")
    # Exactly one shipped prompt is expected to fail, and only on clipping: the
    # Tianjin male prompt contains a run of 15 samples pinned at full scale.
    check(
        "only the genuinely clipped vendor prompt fails",
        len(failing) == 1 and {c.key for c in failing[0].checks if c.status == "fail"} == {"clipping"}
        and Path(failing[0].path).name == "tianjin_male_prompt.wav",
        f"{[Path(r.path).name for r in failing]}",
    )

    print("\n2. The user's slice corpus must pass as a whole (purpose=timbre)")
    corpus = ra.analyze_corpus(SLICES, "timbre")
    print(f"    {corpus['total_seconds']}s across {corpus['files']} files -> {corpus['status']}")
    print(f"    aggregate SNR {corpus['aggregate']['estimated_snr_db']} dB, "
          f"high band {corpus['aggregate']['high_band_db']} dB")
    check("real corpus is not rejected", corpus["status"] != "fail", str(corpus["blocking"]))
    check("real corpus total duration is measured, not a single slice",
          corpus["total_seconds"] > 200, f"{corpus['total_seconds']}s")
    check("a single slice would fail the same purpose, so the unit matters",
          ra.analyze(SLICES[0], "timbre").status == "fail")

    print("\n3. Ground truth: narrowband source must fail on bandwidth, not on noise")
    bands = []
    for index, source in enumerate(PROMPTS[:6]):
        destination = make_narrowband(source, WORK / f"narrow_{source.name}")
        report = ra.analyze(destination, "synthesis")
        bandwidth = next(c for c in report.checks if c.key == "bandwidth")
        bands.append((source.name, report, bandwidth))
        print(f"    {source.name[:30]:<32} {report.status:<5} "
              f"high={report.metrics['high_band_db']:>8} bandwidth={bandwidth.status}")
    check("every narrowband copy is rejected", all(r.status == "fail" for _, r, _ in bands))
    check("the bandwidth check is the one that fires",
          all(b.status == "fail" for _, _, b in bands))
    margin = min(
        ra.analyze(source, "synthesis").metrics["high_band_db"] for source in PROMPTS[:6]
    ) - max(r.metrics["high_band_db"] for _, r, _ in bands)
    print(f"    separation between the lowest real clip and the highest narrowband copy: {margin:.1f} dB")
    check("bandwidth separation exceeds 20 dB", margin > 20, f"{margin:.1f} dB")

    print("\n4. Ground truth: hard clipping must be rejected")
    for source in PROMPTS[:4]:
        destination = make_clipped(source, WORK / f"clip_{source.name}")
        report = ra.analyze(destination, "synthesis")
        clipping = next(c for c in report.checks if c.key == "clipping")
        print(f"    {source.name[:30]:<32} {report.status:<5} "
              f"run={report.metrics['clipping_run']:>4} ratio={report.metrics['clipping_ratio']:.4f}")
        check(f"{source.name} clipped copy is rejected", clipping.status == "fail")

    print("\n5. Ground truth: a noisy corpus must be rejected, a clean one must not")
    clean = concat(SLICES, WORK / "corpus_clean.wav")
    clean_report = ra.analyze_corpus([clean], "timbre")
    print(f"    clean   {clean_report['status']:<5} SNR={clean_report['aggregate']['estimated_snr_db']} dB")
    check("concatenated clean corpus passes", clean_report["status"] != "fail",
          str(clean_report["blocking"]))

    for snr_db, expected in ((20.0, "flag"), (10.0, "fail")):
        noisy = make_noisy(clean, WORK / f"corpus_noisy{int(snr_db)}.wav", snr_db)
        report = ra.analyze_corpus([noisy], "timbre")
        print(f"    noisy {int(snr_db):>3} dB true SNR -> {report['status']:<5} "
              f"measured SNR={report['aggregate']['estimated_snr_db']} dB")
        # The estimator compresses true SNR, so the assertions are stated in terms of
        # what the gate is expected to do rather than an exact number: audio with
        # noise clearly audible in the timbre blocks, and audio merely on the
        # borderline is flagged rather than silently accepted.
        if expected == "fail":
            check(f"corpus with noise injected at {int(snr_db)} dB is rejected",
                  report["status"] == "fail", str(report["warnings"]))
        else:
            check(f"corpus with noise injected at {int(snr_db)} dB is at least flagged",
                  report["status"] != "ok", str(report["warnings"]))

    print("\n6. Corpus aggregation is duration-weighted, not file-counted")
    lopsided = [SLICES[0]] + SLICES[1:4]  # one short slice plus three
    report = ra.analyze_corpus(lopsided, "timbre")
    per_file_seconds = sum(ra.analyze(p, "timbre").duration_seconds for p in lopsided)
    check("aggregated duration equals the sum of the files",
          abs(report["total_seconds"] - per_file_seconds) < 0.01,
          f"{report['total_seconds']} vs {per_file_seconds:.2f}")

    (WORK / "verify.json").write_text(
        json.dumps(
            {
                "vendor_prompts": [r.to_dict() for r in prompt_reports],
                "corpus": corpus,
                "clean_concat": clean_report,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {WORK / 'verify.json'}")
    if FAILURES:
        print(f"\nFAILED: {len(FAILURES)} check(s)")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())