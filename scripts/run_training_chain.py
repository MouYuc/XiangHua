"""Run the GPT-SoVITS training chain with the Gradio shim active in every child.

``s2_train.py`` imports ``utils``, which reaches ``tools/my_utils.py`` and therefore
``gradio``. The vendored repository assumes Gradio is installed because it ships a WebUI;
the training path itself never uses it. Rather than installing a large UI framework to run
a training script, the same shim used by ``run_dataset_prep.py`` is injected through a
generated ``sitecustomize`` module on ``PYTHONPATH``, which Python imports automatically in
every child interpreter. The vendored files are not modified.

Usage:
    .venv\\Scripts\\python.exe scripts\\run_training_chain.py
    .venv\\Scripts\\python.exe scripts\\run_training_chain.py --stage s2
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.run_dataset_prep import install_gradio_shim  # noqa: E402

BOOT_DIR = Path(__file__).resolve().parent / "_shim_boot"

STAGES: dict[str, list[str]] = {
    "s1": ["GPT_SoVITS/s1_train.py", "--config_file", "TEMP/tmp_s1.yaml"],
    "s2": ["GPT_SoVITS/s2_train.py", "--config", "TEMP/tmp_s2.json"],
}


def ensure_boot_module() -> Path:
    """Write (or refresh) the ``sitecustomize`` that installs the shim in children."""
    BOOT_DIR.mkdir(parents=True, exist_ok=True)
    (BOOT_DIR / "sitecustomize.py").write_text(
        "try:\n"
        "    from scripts.run_dataset_prep import install_gradio_shim\n"
        "    install_gradio_shim()\n"
        "except Exception as exc:  # pragma: no cover - diagnostics only\n"
        "    print(f'[gradio-shim] failed: {exc}')\n",
        encoding="utf-8",
    )
    return BOOT_DIR


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=str(REPO_ROOT / "GPT-SoVITS"))
    parser.add_argument("--stage", choices=["all", *STAGES], default="all")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not (root / "GPT_SoVITS" / "s1_train.py").is_file():
        raise SystemExit(f"不是有效的 GPT-SoVITS 根目录：{root}")

    install_gradio_shim()
    boot = ensure_boot_module()

    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), str(boot), environment.get("PYTHONPATH", "")]
    ).strip(os.pathsep)

    stages = list(STAGES) if args.stage == "all" else [args.stage]
    for stage in stages:
        command = [sys.executable, "-s", *STAGES[stage]]
        print(f"=== running {STAGES[stage][0]} ===", flush=True)
        code = subprocess.call(command, cwd=str(root), env=environment)
        print(f"=== exit={code} for {STAGES[stage][0]} ===", flush=True)
        if code != 0:
            return code
    print("training chain finished", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
