#!/usr/bin/env python3
"""Can this project's voice conversion sing? A measured answer, with the control.

The question matters because the kNN-VC paper (Baas, van Niekerk & Kamper,
Interspeech 2023, arXiv:2305.18975) contains **no singing experiments** - it is a
speech paper - so the singing half of this product is built on an extrapolation. This
script turns the extrapolation into a measurement on real sung audio.

The design is an ablation, because "the converted clip sounds like the target" is two
claims and they have to be separated:

* **melody preservation** - pitch compared against the *source* clip
* **timbre adoption** - speaker similarity against the *target* recording

and two controls for the confounds:

* **identity control**: the source is converted against a matching pool built from
  *itself*. Nearest-neighbour lookup against its own frames returns the source, so
  whatever pitch damage remains is the **vocoder's**, not the conversion's. Without
  this, a poor F0 score cannot be attributed.
* **untouched baseline**: the source measured against the target. If conversion does
  not beat this, it did not move the voice at all.

Source audio is Vocadito (Bittner, Pasalo, Genchel, Yip & Kim, TISMIR 2022): 40 solo
vocal recordings, which is real singing rather than speech read slowly.

Usage:
    .venv\\Scripts\\python.exe scripts\\svc_experiment.py --limit 6 --topk 4
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import numpy as np  # noqa: E402

import knnvc_runtime as runtime  # noqa: E402
import svc_metrics  # noqa: E402
from f0_estimate import estimate  # noqa: E402

DEFAULT_VOCADITO = REPO_ROOT / "output" / "svc_eval" / "vocadito" / "Audio"
DEFAULT_REFERENCE = REPO_ROOT / "GPT-SoVITS" / "custom_data" / "sliced"


def reference_corpus(directory: Path, limit: int = 0) -> list[Path]:
    """The clips that define the target timbre, in a stable order."""
    if directory.is_file():
        return [directory]
    files = sorted(
        path for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in {".wav", ".flac", ".mp3", ".m4a", ".ogg"}
    )
    return files[:limit] if limit else files


def summarize(values: list[float]) -> dict:
    """Median and range, never a bare mean.

    A handful of octave errors on one clip would move a mean and say nothing about
    the other clips, so the median is the headline and the extremes are shown.
    """
    clean = [value for value in values if value is not None and np.isfinite(value)]
    if not clean:
        return {}
    return {
        "n": len(clean),
        "median": round(float(statistics.median(clean)), 4),
        "min": round(float(min(clean)), 4),
        "max": round(float(max(clean)), 4),
    }


def collect(rows: list[dict], label: str, keys: tuple[str, ...]) -> dict:
    """Summarize one metric group, separating untrustworthy clips from bad ones.

    A clip whose pitch curve is degenerate is counted as *unmeasurable*, never folded
    into the numbers: dropping it silently would flatter the result, and scoring it
    would fabricate one.
    """
    usable = [row[label] for row in rows if row[label].get("trustworthy")]
    return {
        "measurable_clips": len(usable),
        "untrustworthy_clips": [
            {"clip": row["clip"], "reason": row[label].get("reason")}
            for row in rows if not row[label].get("trustworthy")
        ],
        "metrics": {
            key: summarize([entry.get(key) for entry in usable]) for key in keys
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vocadito", default=str(DEFAULT_VOCADITO))
    parser.add_argument("--reference-dir", default=str(DEFAULT_REFERENCE))
    parser.add_argument("--reference-limit", type=int, default=0, help="cap the reference corpus")
    parser.add_argument("--reference-file", default=None, help="single target recording for SECS")
    parser.add_argument("--limit", type=int, default=6, help="how many Vocadito clips to convert")
    parser.add_argument("--topk", type=int, default=4)
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "output" / "svc_eval" / "runs"))
    parser.add_argument("--device", default=None)
    parser.add_argument("--f0-method", default="crepe", choices=("crepe", "pyin"))
    parser.add_argument("--json-out", default=str(REPO_ROOT / "output" / "svc_eval" / "report.json"))
    args = parser.parse_args()

    clips = sorted(Path(args.vocadito).glob("*.wav"))[: args.limit]
    if not clips:
        raise SystemExit(f"没有找到歌声素材：{args.vocadito}")
    references = reference_corpus(Path(args.reference_dir), args.reference_limit)
    if not references:
        raise SystemExit(f"参考音目录里没有音频：{args.reference_dir}")
    target_reference = Path(args.reference_file) if args.reference_file else references[0]

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"歌声素材 {len(clips)} 条，参考音 {len(references)} 个文件，topk={args.topk}")
    model = runtime.load_model(device=args.device)
    target_pool = runtime.build_matching_set(
        model, references, cache=out_dir / "target_pool.pt", progress=True
    )
    print(f"目标匹配池：{target_pool.shape[0]} 帧 / {target_pool.shape[0] / runtime.FRAME_RATE:.1f}s")

    embedder = None
    try:
        from speaker_embedding import SpeakerEmbedder

        embedder = SpeakerEmbedder(device=args.device)
        print(f"独立说话人模型已加载（{embedder.device}）")
    except Exception as exc:
        print(f"说话人模型不可用，跳过音色指标：{exc}")

    rows: list[dict] = []
    for index, clip in enumerate(clips, start=1):
        started = time.time()
        waveform = runtime.load_audio_16k(clip)
        # The identity control: a pool of the clip's own frames, with VAD disabled.
        # Trimming would remove exactly the quiet sung frames whose pitch damage the
        # control exists to measure, and nearest-neighbour lookup against its own
        # frames then returns the source, isolating the vocoder's effect.
        self_pool = runtime.features_for_waveform(model, waveform, vad_trigger_level=0.0, progress=False)
        converted = runtime.convert(model, waveform, target_pool, topk=args.topk)
        identity = runtime.convert(model, waveform, self_pool, topk=1)

        converted_path = out_dir / f"{clip.stem}_topk{args.topk}.wav"
        identity_path = out_dir / f"{clip.stem}_identity.wav"
        runtime.save_wav(converted_path, converted)
        runtime.save_wav(identity_path, identity)

        source_track = estimate(clip, args.f0_method, device=args.device) if args.f0_method == "crepe" else estimate(clip, args.f0_method)
        converted_track = (
            estimate(converted_path, args.f0_method, device=args.device)
            if args.f0_method == "crepe" else estimate(converted_path, args.f0_method)
        )
        identity_track = (
            estimate(identity_path, args.f0_method, device=args.device)
            if args.f0_method == "crepe" else estimate(identity_path, args.f0_method)
        )

        row = {
            "clip": clip.stem,
            "seconds": round(waveform.shape[-1] / runtime.SAMPLE_RATE, 2),
            "self_pool_frames": int(self_pool.shape[0]),
            "converted": svc_metrics.pitch_preservation(source_track, converted_track),
            "identity": svc_metrics.pitch_preservation(source_track, identity_track),
            "converted_path": str(converted_path),
            "identity_path": str(identity_path),
        }
        if embedder is not None:
            from speaker_embedding import cosine_similarity

            source_embedding = embedder.embed_file(clip)
            converted_embedding = embedder.embed_file(converted_path)
            identity_embedding = embedder.embed_file(identity_path)
            target_embedding = embedder.embed_file(target_reference)
            row["timbre"] = {
                "secs_converted_vs_target": round(cosine_similarity(converted_embedding, target_embedding), 4),
                "secs_source_vs_target": round(cosine_similarity(source_embedding, target_embedding), 4),
                "secs_identity_vs_target": round(cosine_similarity(identity_embedding, target_embedding), 4),
                "secs_converted_vs_source": round(cosine_similarity(converted_embedding, source_embedding), 4),
            }
            row["timbre"]["secs_improvement"] = round(
                row["timbre"]["secs_converted_vs_target"] - row["timbre"]["secs_source_vs_target"], 4
            )
        rows.append(row)

        def describe(entry: dict) -> str:
            if not entry.get("trustworthy"):
                return "不可信"
            return f"{entry.get('median_abs_cents')}"

        print(
            f"  [{index}/{len(clips)}] {clip.stem}: {row['seconds']}s, "
            f"中位音分误差 转换={describe(row['converted'])} "
            f"恒等={describe(row['identity'])}, "
            f"用时 {time.time() - started:.1f}s"
        )

    pitch_keys = (
        "median_abs_cents",
        "raw_pitch_accuracy",
        "raw_chroma_accuracy",
        "octave_error_rate",
        "correlation",
        "median_transposition_cents",
        "vuv_error_rate",
    )
    aggregate = {
        "clips": len(rows),
        "topk": args.topk,
        "f0_method": args.f0_method,
        "target_reference": str(target_reference),
        "target_pool_frames": int(target_pool.shape[0]),
        "converted": collect(rows, "converted", pitch_keys),
        "identity": collect(rows, "identity", pitch_keys),
        "rows": rows,
    }
    timbre_rows = [row for row in rows if "timbre" in row]
    if timbre_rows:
        aggregate["timbre"] = {
            "clips": len(timbre_rows),
            **{
                key: summarize([row["timbre"].get(key) for row in timbre_rows])
                for key in (
                    "secs_converted_vs_target",
                    "secs_source_vs_target",
                    "secs_identity_vs_target",
                    "secs_converted_vs_source",
                    "secs_improvement",
                )
            },
        }

    print("\n=== 汇总（中位数 / 范围） ===")
    for label in ("converted", "identity"):
        entry = aggregate[label]
        print(f"{label}: 可测 {entry['measurable_clips']}/{len(rows)} 条，"
              f"不可信 {len(entry['untrustworthy_clips'])} 条")
        for key, stats in entry["metrics"].items():
            if stats:
                print(f"  {key:<26} 中位 {stats['median']:<10} [{stats['min']}, {stats['max']}]")
    if "timbre" in aggregate:
        print("timbre:")
        for key, stats in aggregate["timbre"].items():
            if isinstance(stats, dict) and "median" in stats:
                print(f"  {key:<28} 中位 {stats['median']:<10} [{stats['min']}, {stats['max']}]")

    output = Path(args.json_out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())