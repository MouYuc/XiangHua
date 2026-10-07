#!/usr/bin/env python3
"""Train a speaker adapter on top of the frozen DiaMoE dialect model.

The released DiaMoE "LoRA" path does not freeze the DiT feed-forward blocks, so
out of the box it trains ~283M parameters. For a speaker adapter that has to fit
next to 8GB of VRAM and only ~1-5 minutes of enrolment audio, this driver can
restrict training to the LoRA matrices only (``--freeze adapter``).

Prep once:

    python scripts/prepare_diamoe_lora_base.py \
        --source third_party/DiaMoE-TTS/checkpoints/10ep_mlpEXP_9.pt \
        --output third_party/DiaMoE-TTS/checkpoints/diamoetts_base_0.pt

Then:

    python scripts/train_diamoe_lora.py \
        --diamoe-root third_party/DiaMoE-TTS \
        --dataset user_standard --freeze adapter --dry-run

Drop ``--dry-run`` to actually train.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import torch
from omegaconf import OmegaConf


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diamoe-root", default="third_party/DiaMoE-TTS")
    parser.add_argument("--config", default="diamoetts_peft.yaml",
                        help="config file under diamoe_tts/src/f5_tts/configs")
    parser.add_argument("--dataset", required=True,
                        help="folder name under diamoe_tts/data (built by build_diamoe_lora_dataset.py)")
    parser.add_argument("--arrow-name", default="dataset")
    parser.add_argument("--basic-ckpt", default="checkpoints/diamoetts_base_0.pt",
                        help="relative to --diamoe-root; name must end with _<int>.pt")
    parser.add_argument("--save-dir", default="lora_runs/speaker")
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--lr", type=float, default=1e-5)
    parser.add_argument("--batch-size", type=int, default=1600,
                        help="batch_size_per_gpu; frame-typed batches measure mel frames")
    parser.add_argument("--max-samples", type=int, default=8)
    parser.add_argument("--grad-accum", type=int, default=1)
    parser.add_argument("--num-workers", type=int, default=0)
    parser.add_argument("--warmup-updates", type=int, default=50)
    parser.add_argument("--save-per-updates", type=int, default=200)
    parser.add_argument("--freeze", choices=["none", "adapter"], default="adapter")
    parser.add_argument("--logger", choices=["none", "tensorboard"], default="none")
    parser.add_argument("--checkpoint-activations", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="build model and dataset, report the plan, then exit")
    return parser.parse_args()


def build_model(cfg, vocab_path: Path):
    """Mirror DiaMoE train.py: CFM + DiT_peft + MoE experts."""
    from f5_tts.model import CFM, DiT_peft
    from f5_tts.model.moe import SimpleGate, MoeLayer, EXPERT_DICT
    from f5_tts.model.utils import get_tokenizer

    vocab_char_map, vocab_size = get_tokenizer(str(vocab_path), "custom")

    moe_cfg = cfg.MoE
    expert_cls = EXPERT_DICT[moe_cfg.expert_type.lower()]
    experts = [expert_cls() for _ in range(moe_cfg.num_exps)]
    moe = MoeLayer(
        experts=experts,
        gate=SimpleGate(moe_cfg.num_exps),
        num_experts=moe_cfg.num_exps,
        num_experts_per_tok=moe_cfg.moe_topK,
        use_residual=True,
        use_dialect_clf=moe_cfg.use_dialect_clf,
        dialect_clf_lambda=moe_cfg.dialect_clf_lambda,
        dialect_kinds=moe_cfg.dialect_kinds,
    )

    model = CFM(
        transformer=DiT_peft(
            **cfg.model.arch,
            text_num_embeds=vocab_size,
            mel_dim=cfg.model.mel_spec.n_mel_channels,
            moe=moe,
        ),
        mel_spec_kwargs=cfg.model.mel_spec,
        vocab_char_map=vocab_char_map,
        use_moe=cfg.MoE.use_moe,
    )
    return model


def restrict_to_adapter(model) -> tuple[int, int, int]:
    """Freeze everything except the LoRA matrices and the text conditioning adapters.

    ``ConditioningAdapter`` is kaiming-initialised rather than zero-initialised,
    so leaving it frozen injects untrained random noise into the text path.
    Returns (frozen, kept, kept_params).
    """
    frozen = kept = kept_params = 0
    for name, param in model.named_parameters():
        if (
            name.endswith("lora_A")
            or name.endswith("lora_B")
            or ".adapter." in name
        ):
            param.requires_grad = True
            kept += 1
            kept_params += param.numel()
        else:
            param.requires_grad = False
            frozen += 1
    return frozen, kept, kept_params


def load_user_dataset(data_dir: Path, arrow_name: str, cfg):
    """Build DiaMoE's CustomDataset directly.

    ``f5_tts.model.dataset.load_dataset`` unconditionally prints
    ``train_dataset[20]``, which crashes on enrolment sets shorter than 21 rows.
    """
    from datasets import Dataset as Dataset_
    from f5_tts.model.dataset import CustomDataset

    raw = Dataset_.from_file(str(data_dir / f"{arrow_name}.arrow"))
    durations = json.loads(
        (data_dir / f"{arrow_name}_duration.json").read_text(encoding="utf-8")
    )["duration"]
    return CustomDataset(
        raw,
        durations=durations,
        preprocessed_mel=False,
        mel_spec_module=None,
        **OmegaConf.to_container(cfg.model.mel_spec, resolve=True),
    )


def main() -> int:
    args = parse_args()
    diamoe_root = Path(args.diamoe_root).resolve()
    src_root = diamoe_root / "diamoe_tts" / "src"
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

    config_path = src_root / "f5_tts" / "configs" / args.config
    cfg = OmegaConf.load(config_path)
    if args.checkpoint_activations:
        cfg.model.arch.checkpoint_activations = True

    vocab_path = diamoe_root / "diamoe_tts" / "data" / "vocab.txt"
    data_dir = diamoe_root / "diamoe_tts" / "data" / args.dataset
    arrow_path = data_dir / f"{args.arrow_name}.arrow"
    duration_path = data_dir / f"{args.arrow_name}_duration.json"
    basic_ckpt = (diamoe_root / args.basic_ckpt).resolve()
    save_dir = (diamoe_root / args.save_dir).resolve()

    for label, path in (
        ("vocab", vocab_path),
        ("arrow", arrow_path),
        ("durations", duration_path),
        ("basic checkpoint", basic_ckpt),
    ):
        if not path.exists():
            raise SystemExit(f"missing {label}: {path}")

    from f5_tts.model import Trainer

    model = build_model(cfg, vocab_path)
    total = sum(p.numel() for p in model.parameters())

    if args.freeze == "adapter":
        frozen, kept, kept_params = restrict_to_adapter(model)
    else:
        frozen, kept = 0, sum(1 for _ in model.named_parameters())
        kept_params = sum(p.numel() for p in model.parameters() if p.requires_grad)

    report = {
        "config": str(config_path),
        "dataset": str(data_dir),
        "rows": json.loads(duration_path.read_text(encoding="utf-8"))["duration"].__len__(),
        "basic_ckpt": str(basic_ckpt),
        "save_dir": str(save_dir),
        "total_params": total,
        "trainable_tensors": kept,
        "trainable_params": kept_params,
        "frozen_tensors": frozen,
        "freeze_mode": args.freeze,
        "epochs": args.epochs,
        "lr": args.lr,
        "batch_size_per_gpu": args.batch_size,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))

    # DiaMoE's trainer and dataset loader assume the package root as cwd.
    os.chdir(diamoe_root / "diamoe_tts")

    train_dataset = load_user_dataset(data_dir, args.arrow_name, cfg)
    print(f"dataset rows: {len(train_dataset)}")

    if args.dry_run:
        print("dry run: model, dataset and paths verified, exiting before training")
        return 0

    trainer = Trainer(
        model,
        epochs=args.epochs,
        learning_rate=args.lr,
        num_warmup_updates=args.warmup_updates,
        save_per_updates=args.save_per_updates,
        keep_last_n_checkpoints=3,
        checkpoint_path=str(save_dir),
        batch_size_per_gpu=args.batch_size,
        batch_size_type=cfg.datasets.batch_size_type,
        max_samples=args.max_samples,
        grad_accumulation_steps=args.grad_accum,
        max_grad_norm=cfg.optim.max_grad_norm,
        logger=None if args.logger == "none" else args.logger,
        wandb_project="DiaMoE-Speaker",
        wandb_run_name=f"speaker_{args.dataset}",
        last_per_updates=args.save_per_updates,
        log_samples=False,
        bnb_optimizer=False,
        mel_spec_type=cfg.model.mel_spec.mel_spec_type,
        is_local_vocoder=cfg.model.vocoder.is_local,
        local_vocoder_path=cfg.model.vocoder.local_path,
        # resolve=False: the shipped config uses Hydra's ${now:...} resolver.
        model_cfg_dict=OmegaConf.to_container(cfg, resolve=False),
        basic_ckpt_path=str(basic_ckpt),
        moe_args=cfg.MoE,
        use_old_scheduler=cfg.optim.use_old_scheduler,
    )

    trainer.train(train_dataset, num_workers=args.num_workers, resumable_with_seed=666)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())