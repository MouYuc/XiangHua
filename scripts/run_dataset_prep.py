#!/usr/bin/env python3
"""Run GPT-SoVITS's dataset preparation without installing Gradio.

Why this exists
---------------

``GPT-SoVITS/tools/my_utils.py`` imports ``gradio`` at module scope, and the four
``GPT_SoVITS/prepare_datasets/*.py`` scripts import ``my_utils``. So **every** dataset step
fails with ``ModuleNotFoundError: No module named 'gradio'`` even though nothing in the
data path touches the web UI.

The repository's own dependency check (``tools.train.missing_deps``) does not catch this,
which is why the training page could report "dependencies satisfied" and then fail at the
first step. That gap is the reason this runner exists rather than a note telling the user
to install two gigabytes of UI framework.

What is shimmed and why that is safe
------------------------------------

Gradio is used in ``my_utils`` only by ``check_for_existance`` and ``check_details``, whose
entire use of it is ``gr.Warning(message)`` - a user-facing notification in the Gradio UI.
Neither function is called by any ``prepare_datasets`` script, and no other Gradio symbol is
referenced. The shim therefore provides exactly ``Warning`` (printed) and nothing else, so
an accidental future dependency on real Gradio behaviour fails loudly instead of silently
returning a mock.

It is installed into ``sys.modules`` only inside this process. The vendored repository is
not modified, so anything else that genuinely needs Gradio is unaffected.

Usage:
    .venv\\Scripts\\python.exe scripts\\run_dataset_prep.py --step text
    .venv\\Scripts\\python.exe scripts\\run_dataset_prep.py --step all
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Version reported by the shim, so a log makes it obvious Gradio was not really installed.
SHIM_VERSION = "0.0.0-dsh-shim"


def install_gradio_shim() -> None:
    """Make ``import gradio as gr`` succeed with only ``Warning`` available."""
    try:
        import gradio  # noqa: F401

        return  # a real installation wins; never shadow it
    except ImportError:
        pass

    module = types.ModuleType("gradio")
    module.__version__ = SHIM_VERSION
    module.__doc__ = "Minimal stand-in: see scripts/run_dataset_prep.py for the rationale."

    def Warning(message, *args, **kwargs):
        print(f"[gradio-shim] Warning: {message}", flush=True)

    def Info(message, *args, **kwargs):
        print(f"[gradio-shim] Info: {message}", flush=True)

    module.Warning = Warning
    module.Info = Info
    sys.modules["gradio"] = module

    # ``my_utils`` does ``import gradio as gr``, and nothing else in the data path imports
    # Gradio, so registering the module name is enough. Submodules are deliberately absent.
    print(f"[gradio-shim] installed stub gradio {SHIM_VERSION}", flush=True)


def _dataset_config(root: Path, exp: str, corrected_rel: str) -> dict[str, str]:
    """The environment ``tools.train.cmd_feat`` builds, kept in one place."""
    return {
        "inp_text": corrected_rel,
        "inp_wav_dir": "custom_data/sliced",
        "exp_name": exp,
        "i_part": "0",
        "all_parts": "1",
        "opt_dir": "custom_data/dataset",
        "bert_pretrained_dir": "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large",
        "version": "v2Pro",
    }


# Each step plus the extra environment it alone needs, mirroring tools.train.cmd_feat.
STEPS: dict[str, tuple[str, dict[str, str]]] = {
    "text": ("1-get-text.py", {}),
    "hubert": ("2-get-hubert-wav32k.py", {"cnhubert_base_dir": "GPT_SoVITS/pretrained_models/chinese-hubert-base"}),
    "sv": ("2-get-sv.py", {"sv_path": "GPT_SoVITS/pretrained_models/sv/pretrained_eres2netv2w24s4ep4.ckpt"}),
    "semantic": (
        "3-get-semantic.py",
        {
            "pretrained_s2G": "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth",
            "s2config_path": "GPT_SoVITS/configs/s2v2Pro.json",
        },
    ),
}
STEP_ORDER = ["text", "hubert", "sv", "semantic"]


SHIM_BOOT_DIR = REPO_ROOT / "scripts" / "_shim_boot"


def ensure_shim_boot() -> Path:
    """Write the ``sitecustomize`` that installs the Gradio stub in child interpreters.

    A child interpreter imports ``sitecustomize`` automatically, which is the only way
    to inject the shim into a subprocess without editing the vendored tree. Shared by
    this CLI and ``tools/train.py`` (the API path runs the same scripts), so the two
    entry points cannot drift apart.
    """
    SHIM_BOOT_DIR.mkdir(parents=True, exist_ok=True)
    (SHIM_BOOT_DIR / "sitecustomize.py").write_text(
        "try:\n"
        "    from scripts.run_dataset_prep import install_gradio_shim\n"
        "    install_gradio_shim()\n"
        "except Exception as exc:  # pragma: no cover - diagnostics only\n"
        "    print(f'[gradio-shim] failed: {exc}')\n",
        encoding="utf-8",
    )
    return SHIM_BOOT_DIR


def run_step(script: str, extra: dict[str, str], root: Path, exp: str, corrected_rel: str) -> int:
    """Run one preparation script as a child process, with the shim already active."""
    environment = dict(os.environ)
    environment.update(_dataset_config(root, exp, corrected_rel))
    environment.update(extra)
    environment["PYTHONIOENCODING"] = "utf-8"
    boot_dir = ensure_shim_boot()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), str(boot_dir), environment.get("PYTHONPATH", "")]
    ).strip(os.pathsep)

    command = [sys.executable, "-s", f"GPT_SoVITS/prepare_datasets/{script}"]
    print(f"[run] {' '.join(command)}  (cwd={root})", flush=True)
    return subprocess.call(command, cwd=str(root), env=environment)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=str(REPO_ROOT / "GPT-SoVITS"))
    parser.add_argument("--exp", default="myvoice")
    parser.add_argument("--corrected", default="custom_data/asr/sliced_corrected.list")
    parser.add_argument("--step", choices=["all", *STEP_ORDER], default="all")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not (root / "GPT_SoVITS" / "prepare_datasets").is_dir():
        raise SystemExit(f"不是有效的 GPT-SoVITS 根目录：{root}")

    install_gradio_shim()

    steps = STEP_ORDER if args.step == "all" else [args.step]
    for step in steps:
        script, extra = STEPS[step]
        code = run_step(script, extra, root, args.exp, args.corrected)
        print(f"[step {step}] exit={code}", flush=True)
        if code != 0:
            return code
    print("数据集准备全部完成", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
