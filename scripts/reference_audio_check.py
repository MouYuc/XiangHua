#!/usr/bin/env python3
"""Objective reference-audio quality gate, as a CLI.

Runs the same analysis the WebUI uses, so a batch audit and an upload see
identical numbers. Two populations matter for calibration and both are in-repo:

* the vendor's own eighteen official DiaMoE prompts - a gate that rejects the
  shipped prompts is miscalibrated by definition, so they are the reference
  population for the ``synthesis`` purpose;
* the user's own slice corpus - the population the ``timbre`` purpose exists for.

Usage:
    .venv\\Scripts\\python.exe scripts\\reference_audio_check.py --prompts
    .venv\\Scripts\\python.exe scripts\\reference_audio_check.py --slices
    .venv\\Scripts\\python.exe scripts\\reference_audio_check.py path\\to\\clip.wav --purpose singing
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services.reference_audio import PURPOSES, analyze, analyze_corpus  # noqa: E402

PROMPTS_DIR = REPO_ROOT / "third_party" / "DiaMoE-TTS" / "prompts"
SLICES_DIR = REPO_ROOT / "GPT-SoVITS" / "custom_data" / "sliced"

AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".m4a", ".ogg"}


def collect(targets: list[str], prompts: bool, slices: bool) -> list[Path]:
    paths: list[Path] = []
    for raw in targets:
        candidate = Path(raw)
        if candidate.is_dir():
            paths.extend(
                path for path in sorted(candidate.iterdir())
                if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES
            )
        elif candidate.is_file():
            paths.append(candidate)
    if prompts:
        paths.extend(sorted(PROMPTS_DIR.glob("*.wav")))
    if slices:
        paths.extend(
            path for path in sorted(SLICES_DIR.iterdir())
            if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES
        )
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("targets", nargs="*", help="files or directories to check")
    parser.add_argument("--purpose", choices=sorted(PURPOSES), default="synthesis")
    parser.add_argument("--prompts", action="store_true", help="include the vendor's official prompts")
    parser.add_argument("--slices", action="store_true", help="include the user's slice corpus")
    parser.add_argument(
        "--corpus",
        action="store_true",
        help="judge everything as ONE corpus instead of file by file; this is the "
             "right unit for timbre/singing, where 64 short slices are pooled",
    )
    parser.add_argument("--with-f0", action="store_true", help="add the pitch-coverage check")
    parser.add_argument("--json-out", default=None)
    parser.add_argument("--quiet", action="store_true", help="only print the summary table")
    args = parser.parse_args()

    paths = collect(args.targets, args.prompts, args.slices)
    if not paths:
        parser.error("no audio given: pass files/directories, or --prompts / --slices")

    if args.corpus:
        report = analyze_corpus(paths, args.purpose)
        print(f"corpus of {report['files']} files, {report['total_seconds']}s "
              f"-> {report['status'].upper()}")
        print(f"  per file: {report['per_file_counts']}")
        aggregate = report["aggregate"]
        print(
            f"  aggregate SNR≈{aggregate['estimated_snr_db']} dB · "
            f"安静段余量 {aggregate['quiet_headroom_db']} dB · "
            f"高频低 {aggregate['high_band_db']} dB · "
            f"削波 {aggregate['clipping_ratio'] * 100:.3f}%"
        )
        for check in report["checks"]:
            mark = {"ok": "通过", "warn": "警告", "fail": "不合格"}[check["status"]]
            print(f"  [{mark}] {check['label']}：{check['message']}")
        worst = report["worst_file"]
        print(f"  worst file: {Path(worst['path']).name} ({worst['status']})")
        if args.json_out:
            Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json_out).write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"wrote {args.json_out}")
        return 0 if report["status"] != "fail" else 1

    reports = []
    for path in paths:
        try:
            report = analyze(path, args.purpose, with_f0=args.with_f0)
        except Exception as exc:  # a corrupt file must not stop a batch audit
            print(f"{path.name}: 无法分析（{type(exc).__name__}: {exc}）")
            continue
        reports.append(report)
        if not args.quiet:
            print(f"\n===== {path.name} — {report.status.upper()} =====")
            print(
                f"  {report.duration_seconds:.2f}s @ {report.sample_rate} Hz / "
                f"{report.channels}ch"
            )
            metrics = report.metrics
            print(
                f"  峰值 {metrics['peak_dbfs']:.1f} dBFS · 有效值 {metrics['rms_dbfs']:.1f} dBFS · "
                f"静音 {metrics['silence_ratio'] * 100:.0f}% · "
                f"SNR≈{metrics['estimated_snr_db']} dB · "
                f"带宽 {metrics['rolloff_99_hz']} Hz"
            )
            for check in report.checks:
                if check.status == "ok":
                    continue
                mark = "警告" if check.status == "warn" else "不合格"
                print(f"  [{mark}] {check.label}：{check.message}")

    if not reports:
        return 1

    header = (
        f"{'file':<38}{'status':>7}{'sec':>8}{'rate':>7}{'peak':>8}{'rms':>8}"
        f"{'sil':>6}{'snr':>8}{'4k+':>9}"
    )
    print()
    print(header)
    print("-" * len(header))
    for report in reports:
        metrics = report.metrics
        # An unmeasurable SNR prints as "n/a"; a numeric sentinel would read as data.
        snr = metrics["estimated_snr_db"]
        snr_text = f"{snr:.1f}" if snr is not None else "n/a"
        high = metrics["high_band_db"]
        high_text = f"{high:.1f}" if high is not None else "n/a"
        print(
            f"{Path(report.path).name[:37]:<38}{report.status:>7}"
            f"{report.duration_seconds:>8.2f}{report.sample_rate:>7}"
            f"{metrics['peak_dbfs']:>8.1f}{metrics['rms_dbfs']:>8.1f}"
            f"{metrics['silence_ratio'] * 100:>5.0f}%"
            f"{snr_text:>8}{high_text:>9}"
        )

    counts = {"ok": 0, "warn": 0, "fail": 0}
    for report in reports:
        counts[report.status] += 1
    print(f"\n{len(reports)} clips: {counts['ok']} ok, {counts['warn']} warn, {counts['fail']} fail")

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            json.dumps([report.to_dict() for report in reports], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())