"""Verify the seed-reproducibility claim in a way that matches how it is true.

``docs/PROJECT_MEMORY.md`` records that two full regressions with the same
configuration produced byte-identical output. That was measured across two
*separate processes*, and on 2026-11-01 the boundary turned out to matter:

* two fresh processes, same seed  → identical PCM (reproduced here);
* two calls in one warm process   → different PCM.

So "same seed ⇒ same bytes" holds per process, not per call. This script makes
that precise instead of leaving it as folklore. It runs one short synthesis in
each of N fresh subprocesses and compares the decoded PCM hashes.

    .venv\\Scripts\\python.exe scripts\\seed_determinism_check.py --runs 2

Exit code 0 when all runs agree, 1 otherwise (so it can gate a release check).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_TEXT = "今天天气真不错，我们出去走走吧。"

# Runs inside a fresh interpreter: keep it dependency-light and print one JSON line.
CHILD_SCRIPT = r"""
import hashlib, json, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import numpy as np
import soundfile as sf
from backend.services import core

text, seed, take_selection = sys.argv[2], int(sys.argv[3]), sys.argv[4] == "1"
cfg = dict(core.load_config())
cfg["seed"] = seed
cfg["take_selection"] = take_selection
with tempfile.TemporaryDirectory() as raw:
    result = core.synthesize_lines(cfg, text, Path(raw))
    target = Path(result["outputs"][0]["file"])
    data, rate = sf.read(str(target), dtype="float32")
    print(json.dumps({
        "pcm_sha256": hashlib.sha256(np.ascontiguousarray(data).tobytes()).hexdigest(),
        "file_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "duration": round(len(data) / rate, 3),
        "suffix": target.suffix,
    }))
"""


def run_once(text: str, seed: int, take_selection: bool) -> dict:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            CHILD_SCRIPT,
            str(REPO_ROOT),
            text,
            str(seed),
            "1" if take_selection else "0",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise SystemExit(f"子进程失败：\n{completed.stderr[-2000:]}")
    line = [item for item in completed.stdout.splitlines() if item.startswith("{")]
    if not line:
        raise SystemExit(f"子进程没有输出结果：\n{completed.stdout[-2000:]}")
    return json.loads(line[-1])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=2, help="fresh processes to compare (default 2)")
    parser.add_argument("--seed", type=int, default=12345)
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument(
        "--take-selection",
        action="store_true",
        help="keep readback take selection on (off by default: it adds its own seeds)",
    )
    args = parser.parse_args()

    results = []
    for index in range(max(1, args.runs)):
        print(f"[{index + 1}/{args.runs}] 运行中…", flush=True)
        results.append(run_once(args.text, args.seed, args.take_selection))
        print(f"    pcm={results[-1]['pcm_sha256'][:16]} 时长={results[-1]['duration']}s", flush=True)

    pcm = {item["pcm_sha256"] for item in results}
    files = {item["file_sha256"] for item in results}
    durations = {item["duration"] for item in results}

    print()
    print(f"PCM 一致：{'是' if len(pcm) == 1 else '否'}（{len(pcm)} 种）")
    print(f"文件字节一致：{'是' if len(files) == 1 else '否'}（{len(files)} 种）")
    print(f"时长：{sorted(durations)}")
    print()
    if len(pcm) == 1:
        print("结论：跨进程同种子输出一致（记住这是「每次全新进程」的保证，进程内重复调用不保证）。")
        return 0
    print("结论：跨进程输出也不一致——seed 复现性退化，需要排查引擎/CUDA 的确定性设置。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
