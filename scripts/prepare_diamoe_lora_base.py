#!/usr/bin/env python3
"""Convert a DiaMoE base checkpoint into the layout Trainer.load_basic_checkpoint expects.

The released ``10ep_mlpEXP_9.pt`` only carries ``model_state_dict`` with plain
``transformer.*`` keys. The trainer reads ``ema_model_state_dict`` and, on a
fresh run, strips the ``ema_model.`` prefix back off before loading the model,
so the base has to be re-wrapped once here.

The file name must end with ``_<int>.pt`` (for example ``diamoetts_base_0.pt``)
because the trainer parses an update counter out of the file name.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="released DiaMoE .pt checkpoint")
    parser.add_argument("--output", required=True, help="destination, must end with _<int>.pt")
    args = parser.parse_args()

    source = Path(args.source).resolve()
    output = Path(args.output).resolve()
    if not output.stem.split("_")[-1].isdigit():
        raise SystemExit("output name must end with _<int>.pt, e.g. diamoetts_base_0.pt")

    payload = torch.load(source, map_location="cpu", weights_only=True)
    if "model_state_dict" not in payload:
        raise SystemExit(f"{source} has no model_state_dict: {sorted(payload)}")

    state = payload["model_state_dict"]
    wrapped = {
        "ema_model_state_dict": {
            f"ema_model.{key}": value for key, value in state.items()
        }
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    torch.save(wrapped, output)

    report = {
        "source": str(source),
        "output": str(output),
        "tensors": len(state),
        "bytes": output.stat().st_size,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())