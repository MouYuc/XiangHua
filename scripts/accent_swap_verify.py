#!/usr/bin/env python3
"""Controlled accent-swap check on one fixed sentence.

The 20-sentence bench compares different texts across accent groups, so it cannot
prove that the accent level itself caused a CER change. This script holds the text,
reference audio and model fixed, changes only ``accent_level``, and reports CER and
SECS for each level. It is the minimal version of the architecture's
"change accent without changing timbre" exchange test.

The suite itself lives in ``tts_regression.run_swap_suite`` so the standalone tool
and the fixed regression bench share one implementation and one set of constants;
this module only keeps a compatible CLI and report shape.

Usage:
    .venv\\Scripts\\python.exe scripts/accent_swap_verify.py
    .venv\\Scripts\\python.exe scripts/accent_swap_verify.py --skip-synthesis
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from tts_regression import (
    SWAP_BASE_TEXT,
    SWAP_LEVELS,
    CerEvaluator,
    SecsEvaluator,
    run_swap_suite,
)

from backend.services import core

# Re-exported for the contract tests and older callers; the source of truth is
# tts_regression, where the fixed bench runs the same suite.
BASE_TEXT = SWAP_BASE_TEXT
LEVELS = SWAP_LEVELS
DEFAULT_OUT_DIR = REPO_ROOT / "output" / "accent_swap"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--json-out", default=str(DEFAULT_OUT_DIR / "report.json"))
    parser.add_argument("--skip-synthesis", action="store_true", help="reuse audio already under out-dir/<level>/")
    parser.add_argument("--device", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out_root = Path(args.out_dir)
    cfg = core.load_config()
    cfg["auto_lightmid"] = False
    cer_eval = CerEvaluator(args.device)
    secs_eval = SecsEvaluator(cfg.get("ref_audio"), args.device)
    suite = run_swap_suite(
        core,
        cfg,
        out_root,
        skip_synthesis=args.skip_synthesis,
        cer_evaluator=cer_eval,
        secs_evaluator=secs_eval,
    )
    report = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "base_text": suite["base_text"],
        "levels": suite["levels"],
        "config": {key: cfg.get(key) for key in ("gpt_model", "sovits_model", "ref_audio", "ref_text")},
        "rows": suite["rows"],
        "summary": suite["summary"],
    }
    report_path = Path(args.json_out)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report: {report_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
