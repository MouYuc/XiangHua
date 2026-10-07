#!/usr/bin/env python3
"""Synthesise dialect speech with an optional user speaker adapter.

Dialect phonology comes from the frozen DiaMoE base (MoE experts + IPA frontend);
speaker identity comes from the reference audio, optionally refined by a LoRA
adapter trained with ``train_diamoe_lora.py``.

Both the reference and the target text are supplied as IPA lines in DiaMoE's
``id<TAB>text<TAB>ipa`` format, for example the files produced under
``third_party/DiaMoE-TTS/dialect_frontend/work``.

The heavy lifting lives in ``diamoe_runtime.py`` so the same code path is shared
with the batch tools.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))

from diamoe_runtime import (  # noqa: E402
    DEFAULT_BASE_CKPT,
    DEFAULT_ROOT,
    build_model,
    ipa_to_model_text,
    load_symbol_tables,
    prepare_reference,
    read_ipa_row,
    synthesize,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diamoe-root", default=str(DEFAULT_ROOT))
    parser.add_argument("--base-ckpt", default=DEFAULT_BASE_CKPT)
    parser.add_argument("--lora", default=None, help="speaker adapter checkpoint, optional")
    parser.add_argument("--seed", type=int, default=333,
                        help="fixed sampling seed so A/B comparisons are not noise")
    parser.add_argument("--lora-scale", type=float, default=1.0,
                        help="scales lora_B: 0 disables the adapter, 1 is full strength")
    parser.add_argument("--ref-audio", required=True, help="user reference wav")
    parser.add_argument("--ref-ipa-file", required=True)
    parser.add_argument("--ref-line", type=int, default=0)
    parser.add_argument("--gen-ipa-file", required=True)
    parser.add_argument("--gen-line", type=int, default=0)
    parser.add_argument("--out", required=True)
    parser.add_argument("--nfe", type=int, default=32)
    parser.add_argument("--cfg", type=float, default=2.0)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--dump-gate", default=None,
                        help="write MoE gate routing statistics for this utterance to JSON")
    args = parser.parse_args()

    runtime = build_model(
        root=args.diamoe_root,
        base_ckpt=args.base_ckpt,
        lora_path=(Path(args.diamoe_root) / args.lora) if args.lora else None,
        lora_scale=args.lora_scale,
    )

    ipa_set, punctuation = load_symbol_tables(args.diamoe_root)
    ref_id, ref_plain, ref_raw = read_ipa_row(args.ref_ipa_file, args.ref_line)
    gen_id, gen_plain, gen_raw = read_ipa_row(args.gen_ipa_file, args.gen_line)
    ref_text, _, _ = ipa_to_model_text(ref_raw, ipa_set, punctuation)
    gen_text, _, gen_dropped = ipa_to_model_text(gen_raw, ipa_set, punctuation)

    ref_audio, ref_text = prepare_reference(args.ref_audio, ref_text)

    print(json.dumps({
        "base_ckpt": runtime["base_ckpt"],
        "base_load": runtime["base_load"],
        "backbone_config": runtime["backbone_config"],
        "adapter": runtime["adapter"],
        "device": runtime["device"],
        "reference_id": ref_id,
        "reference_text": ref_plain,
        "target_id": gen_id,
        "target_text": gen_plain,
        "dropped_ipa_tokens": gen_dropped,
    }, ensure_ascii=False, indent=2))

    audio, sample_rate, gate = synthesize(
        runtime, ref_audio, ref_text, gen_text,
        seed=args.seed, nfe=args.nfe, cfg_strength=args.cfg, speed=args.speed,
        capture_gate=args.dump_gate is not None, quiet=False,
    )

    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_path), audio, sample_rate)

    if args.dump_gate is not None:
        gate_path = Path(args.dump_gate).resolve()
        gate_path.parent.mkdir(parents=True, exist_ok=True)
        gate_path.write_text(json.dumps(gate, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "out": str(out_path),
        "duration": round(len(audio) / sample_rate, 3),
        "sample_rate": sample_rate,
        "gate_top1_expert": (gate or {}).get("top1_expert"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())