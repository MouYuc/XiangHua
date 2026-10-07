#!/usr/bin/env python3
"""Build a blind listening package from a dialect_baseline candidate set.

``dialect_baseline.py`` writes its candidates as ``..._s333.wav`` / ``..._s1.wav``,
so a listener always knows which seed they are hearing. That is fine for picking a
usable clip, but it cannot tell us whether one seed is genuinely better: knowing
the filename is enough to bias the choice, and any "seed X wins" pattern measured
that way is unfalsifiable.

This tool copies the same audio under neutral names with the position shuffled per
group, and keeps the seed mapping in a separate key file. Re-listening blind is
the only way to find out whether the earlier preference was real.

Position and identity are shuffled independently, so the resulting picks separate
"whoever sits third in the list" from "seed 333 sounds better".
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
from pathlib import Path

DEFAULT_RUN = Path("output/dialect_baseline/20260923_224346")
DEFAULT_LABELS = Path("assets/dialect_listening_labels.json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", default=str(DEFAULT_RUN))
    parser.add_argument("--labels", default=str(DEFAULT_LABELS))
    parser.add_argument("--out-dir", default="output/dialect_baseline/blind_audition")
    parser.add_argument("--shuffle-seed", type=int, default=20260923,
                        help="only fixes the anonymised order, so the package is reproducible")
    parser.add_argument("--probes", default="probe002,probe004")
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    results = json.loads((run_dir / "baseline.json").read_text(encoding="utf-8"))
    labels = json.loads(Path(args.labels).read_text(encoding="utf-8"))
    previous = {(item["dialect"], item["probe"]): item["preferred_seed"] for item in labels["labels"]}
    probe_ids = [item.strip() for item in args.probes.split(",") if item.strip()]

    out_dir = Path(args.out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rng = random.Random(args.shuffle_seed)
    key = {"run_dir": str(run_dir), "shuffle_seed": args.shuffle_seed, "groups": {}}
    lines = [
        "# Blind dialect audition",
        "",
        "Every folder below holds the same candidates you already heard, renamed to `01.wav`..`04.wav`",
        "with the order shuffled per group. Listen in numeric order and note which one sounds best;",
        "do not open `blind_map.json` until you are done.",
        "",
        "| group | folder | candidates | your pick |",
        "| --- | --- | --- | --- |",
    ]

    groups = results["groups"]["user_reference"] + results["groups"]["frontend_only"]
    for entry in groups:
        dialect = entry["dialect"]
        for record in entry["records"]:
            if "candidates" not in record or record["probe"] not in probe_ids:
                continue
            candidates = list(record["candidates"])
            sources = [candidate["wav"] for candidate in candidates]
            order = list(range(len(sources)))
            rng.shuffle(order)

            group_dir = out_dir / f"{dialect}_{record['probe']}"
            group_dir.mkdir(parents=True, exist_ok=True)
            mapping = {}
            for slot, index in enumerate(order):
                name = f"{slot + 1:02d}.wav"
                shutil.copyfile(sources[index], group_dir / name)
                mapping[name] = {
                    "seed": candidates[index]["seed"],
                    "cer": candidates[index]["cer"],
                }
            key["groups"][f"{dialect}_{record['probe']}"] = {
                "reference_text": record["reference_text"],
                "slots": mapping,
                "previous_pick_seed": previous.get((dialect, record["probe"])),
            }
            lines.append(
                f"| {dialect} {record['probe']} | `{group_dir.name}/` | 01-04 |  |"
            )

    (out_dir / "blind_map.json").write_text(json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "LISTEN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({
        "out_dir": str(out_dir),
        "groups": len(key["groups"]),
        "instructions": str(out_dir / "LISTEN.md"),
        "key_do_not_open": str(out_dir / "blind_map.json"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())