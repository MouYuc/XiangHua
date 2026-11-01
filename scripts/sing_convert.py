#!/usr/bin/env python3
"""Convert a source recording (speech or singing) to a reference speaker's timbre.

This is the timbre-transfer half of the product: the user supplies their own
recording once, and any source material - a dialect synthesis, a dubbed line, a
song vocal - comes back in their voice. The conversion itself is untrained
kNN-VC (Baas et al., Interspeech 2023): no fine-tuning, no per-speaker model.

Examples:
    # convert one file using a single reference clip
    python scripts/sing_convert.py --source song_vocal.wav \
        --reference GPT-SoVITS/custom_data/sliced/训练.wav_0000191040_0000343680.wav \
        --out output/vc/song_user.wav

    # use a whole reference directory, caching the encoded frame pool
    python scripts/sing_convert.py --source song_vocal.wav --reference-dir GPT-SoVITS/custom_data/sliced \
        --reference-cache output/vc/user_matching_set.pt --out output/vc/song_user.wav

    # sweep k, which trades source frame variation against timbre match
    python scripts/sing_convert.py --source song_vocal.wav --reference-dir ... --topk 1 2 4 8
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from knnvc_runtime import (
    DEFAULT_CHUNK_SECONDS,
    DEFAULT_KNNVC_ROOT,
    build_matching_set,
    convert,
    features_for_waveform,
    load_audio_16k,
    load_model,
    resolve_device,
    save_wav,
    speaker_embedding_cosine,
)

AUDIO_SUFFIXES = {".wav", ".flac", ".mp3", ".m4a", ".ogg"}


def collect_references(args) -> list[Path]:
    """Resolve every reference source into an explicit, ordered file list."""
    files: list[Path] = []
    for item in args.reference or []:
        path = Path(item)
        if not path.is_file():
            raise SystemExit(f"--reference is not a file: {path}")
        files.append(path)
    for item in args.reference_dir or []:
        directory = Path(item)
        if not directory.is_dir():
            raise SystemExit(f"--reference-dir is not a directory: {directory}")
        found = sorted(
            p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
        )
        if not found:
            raise SystemExit(f"no audio files under {directory}")
        files.extend(found)
    if not files:
        raise SystemExit("no reference audio given; use --reference or --reference-dir")
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="source speech or singing vocal")
    parser.add_argument("--reference", action="append", default=None)
    parser.add_argument("--reference-dir", action="append", default=None)
    parser.add_argument("--reference-cache", default=None,
                        help="store/reuse the encoded target frame pool here")
    parser.add_argument("--out", default=None, help="output wav; required unless --topk has several values")
    parser.add_argument("--topk", type=int, nargs="+", default=[4],
                        help="k for the kNN; the paper's default is 4")
    parser.add_argument("--chunk-seconds", type=float, default=DEFAULT_CHUNK_SECONDS)
    parser.add_argument("--knnvc-root", default=str(DEFAULT_KNNVC_ROOT))
    parser.add_argument("--device", default=None)
    parser.add_argument("--max-reference-files", type=int, default=None,
                        help="cap how many reference files are encoded (fast smoke runs)")
    args = parser.parse_args()

    if args.out is None and len(args.topk) > 1:
        # Several k values need several outputs; derive them from the source name.
        pass
    elif args.out is None:
        raise SystemExit("--out is required when --topk has a single value")

    device = resolve_device(args.device)
    print(f"[device] {device}")

    started = time.time()
    model = load_model(args.knnvc_root, device=str(device))
    print(f"[model] kNN-VC ready in {time.time() - started:.1f}s")

    references = collect_references(args)
    if args.max_reference_files:
        references = references[: args.max_reference_files]
    print(f"[reference] {len(references)} file(s)")

    started = time.time()
    matching_set = build_matching_set(
        model, references, cache=args.reference_cache, chunk_seconds=args.chunk_seconds
    )
    print(
        f"[matching set] {matching_set.shape[0]} frames "
        f"({matching_set.shape[0] / 50.0:.1f}s of audio) in {time.time() - started:.1f}s"
    )

    source_path = Path(args.source)
    if not source_path.is_file():
        raise SystemExit(f"--source is not a file: {source_path}")

    source_wave = load_audio_16k(source_path)
    source_features = features_for_waveform(model, source_wave, vad_trigger_level=0.0)
    source_similarity = speaker_embedding_cosine(source_features, matching_set)
    print(f"[stats] source vs reference timbre similarity: {source_similarity:.4f}")

    for topk in args.topk:
        if args.out is None:
            out_path = Path("output/vc") / f"{source_path.stem}_user_topk{topk}.wav"
        elif len(args.topk) > 1:
            out_path = Path(args.out).with_name(f"{Path(args.out).stem}_topk{topk}.wav")
        else:
            out_path = Path(args.out)

        started = time.time()
        converted = convert(model, source_wave, matching_set, topk=topk)
        elapsed = time.time() - started
        save_wav(out_path, converted)

        out_features = features_for_waveform(model, converted[None], vad_trigger_level=0.0)
        output_similarity = speaker_embedding_cosine(out_features, matching_set)
        duration = converted.shape[-1] / 16000
        print(
            f"[topk={topk}] {duration:.2f}s audio in {elapsed:.1f}s "
            f"-> {out_path}  (output vs reference similarity {output_similarity:.4f})"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())