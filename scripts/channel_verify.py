#!/usr/bin/env python3
"""Cross-channel exchange test (architecture §7, exchange test #4).

"Change the channel, not the person": recordings of the SAME speaker on different
microphones/channels should stay acoustically close in speaker space, and a voice
built from one channel should match every channel's reference.

What it does, per input recording (no manual transcripts required):

1. Runs the standard ``voice_ingest`` auto pipeline (slice → gate → ASR) so each
   channel yields one clean reference clip with its transcript;
2. Embeds every reference clip once and reports the pairwise SECS matrix —
   the same-speaker cross-channel baseline;
3. Synthesizes one fixed sentence from EACH channel's reference and scores that
   output against EVERY channel's reference. If the timbre really is
   channel-independent, each output is close to all references, not just its own.

Usage:
    .venv\\Scripts\\python.exe scripts\\channel_verify.py A.wav B.wav [C.wav ...]
    .venv\\Scripts\\python.exe scripts\\channel_verify.py A.wav B.wav --skip-synthesis
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from tts_regression import REGRESSION_SEED, SWAP_BASE_TEXT, CerEvaluator

DEFAULT_OUT_DIR = REPO_ROOT / "output" / "channel_verify"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("inputs", nargs="+", help="≥2 段同一说话人的录音（不同麦克风/信道）")
    parser.add_argument("--text", default=SWAP_BASE_TEXT, help="用于交叉合成的固定句子")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--json-out", default=None)
    parser.add_argument("--skip-synthesis", action="store_true", help="只算参考片段两两 SECS")
    parser.add_argument("--device", default=None)
    return parser


def summarize_channel(
    ref_matrix_values: list[float], output_spreads: list[float]
) -> dict[str, Any]:
    """Descriptive roll-up; like the swap suite this is not a pass/fail gate."""
    return {
        "ref_pairs": len(ref_matrix_values),
        "ref_secs_mean": round(statistics.fmean(ref_matrix_values), 6) if ref_matrix_values else None,
        "ref_secs_min": round(min(ref_matrix_values), 6) if ref_matrix_values else None,
        "output_secs_spread_mean": (
            round(statistics.fmean(output_spreads), 6) if output_spreads else None
        ),
        "output_secs_spread_max": round(max(output_spreads), 6) if output_spreads else None,
        "note": "同人跨信道：参考两两 SECS 应明显高；每个合成输出对各参考的 SECS 应接近（音色不绑信道）。",
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    inputs = [Path(item) for item in args.inputs]
    if len(inputs) < 2:
        print("至少需要两段录音才能做换信道对比。", file=sys.stderr)
        return 2
    for item in inputs:
        if not item.is_file():
            print(f"录音不存在：{item}", file=sys.stderr)
            return 2
    if len({item.resolve() for item in inputs}) != len(inputs):
        print("输入里有重复文件。", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    from speaker_embedding import SpeakerEmbedder, cosine_similarity

    from backend.services import core, voice_ingest

    refs: list[dict[str, Any]] = []
    for item in inputs:
        try:
            prepared = voice_ingest.prepare_reference_from_audio(
                item, out_dir / f"prepared_{item.stem[:24]}", language=args.language
            )
        except voice_ingest.VoiceIngestError as exc:
            print(f"自动建档失败 {item.name}：{exc}", file=sys.stderr)
            return 2
        quality = prepared.get("quality") or {}
        refs.append(
            {
                "channel": item.stem,
                "source": str(item),
                "ref_audio": prepared["ref_audio"],
                "ref_text": prepared.get("ref_text") or "",
                "quality_status": quality.get("status"),
                "selected": prepared.get("selected") or {},
            }
        )
        print(
            f"ref[{item.stem}] {Path(prepared['ref_audio']).name} "
            f"quality={quality.get('status')} text={prepared.get('ref_text', '')[:24]}"
        )

    # One embedder, one embedding per clip: pairwise reference SECS matrix.
    embedder = SpeakerEmbedder(device=args.device)
    embeddings = {ref["channel"]: embedder.embed_file(ref["ref_audio"]) for ref in refs}
    channels = [ref["channel"] for ref in refs]
    ref_matrix: dict[str, dict[str, Any]] = {}
    ref_matrix_values: list[float] = []
    for left in channels:
        row: dict[str, Any] = {}
        for right in channels:
            if left == right:
                row[right] = None
                continue
            value = round(float(cosine_similarity(embeddings[left], embeddings[right])), 6)
            row[right] = value
            ref_matrix_values.append(value)
        ref_matrix[left] = row
    print("参考两两 SECS：")
    for left in channels:
        cells = "  ".join(
            f"{right}={ref_matrix[left][right]}" if ref_matrix[left][right] is not None else f"{right}=—"
            for right in channels
        )
        print(f"  {left}: {cells}")

    output_rows: list[dict[str, Any]] = []
    output_spreads: list[float] = []
    cer_eval = CerEvaluator(args.device)
    if not args.skip_synthesis:
        cfg = dict(core.load_config())
        cfg["auto_lightmid"] = False
        cfg["accent_level"] = "off"
        cfg["seed"] = REGRESSION_SEED
        for ref in refs:
            channel_dir = out_dir / f"synth_{ref['channel'][:24]}"
            run_cfg = {
                **cfg,
                "ref_audio": ref["ref_audio"],
                "ref_text": ref["ref_text"],
                "prompt_lang": args.language,
            }
            try:
                result = core.synthesize_lines(run_cfg, args.text, output_dir=channel_dir)
                output = (result.get("outputs") or [{}])[0]
                audio_path = str(output.get("file") or "")
                error = output.get("error") or ""
            except Exception as exc:
                audio_path, error = "", f"{type(exc).__name__}: {exc}"
            row: dict[str, Any] = {
                "channel": ref["channel"],
                "file": audio_path,
                "error": error,
                "cer": {"status": "skipped", "reason": error or "没有音频"},
                "secs_to_ref": {},
            }
            if audio_path and Path(audio_path).is_file():
                row["cer"] = cer_eval.evaluate(args.text, audio_path)
                output_embedding = embedder.embed_file(audio_path)
                values: list[float] = []
                for other in channels:
                    value = round(
                        float(cosine_similarity(embeddings[other], output_embedding)), 6
                    )
                    row["secs_to_ref"][other] = value
                    values.append(value)
                if values:
                    output_spreads.append(round(max(values) - min(values), 6))
            output_rows.append(row)
            print(
                f"synth[{ref['channel']}] "
                f"CER={row['cer'].get('value') if row['cer'].get('status') == 'ok' else 'skip'} "
                f"SECS={row['secs_to_ref'] or 'skip'}"
            )

    report = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "text": args.text,
        "language": args.language,
        "skip_synthesis": bool(args.skip_synthesis),
        "refs": refs,
        "ref_matrix": ref_matrix,
        "outputs": output_rows,
        "summary": summarize_channel(ref_matrix_values, output_spreads),
    }
    report_path = Path(args.json_out) if args.json_out else out_dir / "report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report: {report_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
