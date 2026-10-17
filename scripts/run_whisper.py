# -*- coding: utf-8 -*-
"""用 faster-whisper 对切片做识别，生成 GPT-SoVITS 训练标注。

用法：
    python run_whisper.py --input 切片目录 --output 输出目录 --model large-v3
    python run_whisper.py --input 切片目录 --output 输出目录 --model-dir 本地模型目录
输出：<output>/<input目录名>_whisper.list（格式：wav路径|说话人|ZH|文本）

为什么要支持本地模型目录
------------------------
faster-whisper 的 ``WhisperModel("large-v3")`` 会去 HuggingFace 下载权重。
本项目的运行环境**无法访问 HuggingFace**（443 超时），所以那一行会直接失败，
整条训练链路第一步就断了。ModelScope 上同一个模型是可达的，因此这里优先使用
显式给出的本地目录，其次按下面的顺序自动查找，最后才回落到按名字下载。

查找顺序：
1. ``--model-dir`` 显式指定；
2. 环境变量 ``GUANGPU_WHISPER_DIR``；
3. 仓库内 ``.guangpu_webui/models/models/Systran--faster-whisper-<name>/snapshots/*``；
4. 用户级 ModelScope 缓存 ``~/.cache/modelscope/hub/models/Systran--faster-whisper-<name>``；
5. 原行为：把 ``--model`` 当模型名交给 faster-whisper（需要能访问 HuggingFace）。
"""

import argparse
import os
import sys
from pathlib import Path

from faster_whisper import WhisperModel
from tqdm import tqdm

REPO_ROOT = Path(__file__).resolve().parent.parent

# faster-whisper 加载一个模型目录时需要的文件。用它判断目录是不是一个完整模型，
# 而不是"目录存在就算数"——半下载的目录会让加载以一个难懂的异常失败。
REQUIRED_MODEL_FILES = ("model.bin", "config.json", "tokenizer.json")

# 短名 -> faster-whisper 官方仓库名。命令里默认写的是短名 ``large-v3``，
# 而 ModelScope / HuggingFace 上的目录名是 ``Systran--faster-whisper-large-v3``，
# 不映射就永远找不到本地副本，只能回落到联网下载。
KNOWN_MODEL_REPOS = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v1": "Systran/faster-whisper-large-v1",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large": "Systran/faster-whisper-large-v3",
}


def _looks_like_model_dir(path: Path) -> bool:
    return path.is_dir() and all((path / name).is_file() for name in REQUIRED_MODEL_FILES)


def _candidate_directories(model_name: str) -> list[Path]:
    """把各种可能的缓存目录名都列出来。"""
    repo = KNOWN_MODEL_REPOS.get(model_name, model_name)
    # ModelScope 把斜杠换成两个连字符，下划线也可能被写成连字符。
    stems = {model_name, repo}
    for name in (model_name, repo):
        stems.add(name.replace("/", "--"))
        stems.add(name.replace("/", "--").replace("_", "-"))

    bases = [
        REPO_ROOT / ".guangpu_webui" / "models" / "models",
        Path.home() / ".cache" / "modelscope" / "hub" / "models",
        Path.home() / ".cache" / "huggingface" / "hub",
    ]
    directories: list[Path] = []
    for stem in sorted(stems):
        for base in bases:
            directory = base / stem
            if directory.is_dir():
                directories.append(directory)
        # 兜底：按名字里的关键词模糊匹配，覆盖本项目没预料到的命名。
        short = stem.split("/")[-1].split("--")[-1]
        for base in bases:
            if base.is_dir():
                directories.extend(
                    path for path in base.glob(f"*{short}*") if path.is_dir()
                )
    return directories


def resolve_local_model(model_name: str) -> Path | None:
    """按上面的顺序找一个可用的本地模型目录，找不到返回 None。"""
    from_env = os.environ.get("GUANGPU_WHISPER_DIR")
    candidates: list[Path] = []
    if from_env:
        candidates.append(Path(from_env))

    for directory in _candidate_directories(model_name):
        snapshots = directory / "snapshots"
        if snapshots.is_dir():
            candidates.extend(sorted(path for path in snapshots.iterdir() if path.is_dir()))
        candidates.append(directory)

    for candidate in candidates:
        if _looks_like_model_dir(candidate):
            return candidate
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True, help="切片 wav 目录（32k 单声道）")
    p.add_argument("--output", required=True, help="标注输出目录")
    p.add_argument("--model", default="large-v3", help="whisper 模型名（本地找不到时才会去下载）")
    p.add_argument("--model-dir", default=None, help="本地模型目录，优先于 --model")
    p.add_argument("--language", default="zh")
    p.add_argument("--device", default="cuda", help="cuda 或 cpu")
    p.add_argument("--compute-type", default="float16", help="float16 / int8_float16 / int8")
    args = p.parse_args()

    if args.model_dir:
        model_ref = Path(args.model_dir)
        if not _looks_like_model_dir(model_ref):
            raise SystemExit(
                f"--model-dir 不是一个完整的 faster-whisper 模型目录：{model_ref}"
                f"（需要 {', '.join(REQUIRED_MODEL_FILES)}）"
            )
    else:
        local = resolve_local_model(args.model)
        if local is not None:
            model_ref = local
            print(f"[whisper] 使用本地模型：{model_ref}", flush=True)
        else:
            model_ref = args.model
            print(
                f"[whisper] 本地没有找到 {args.model}，交给 faster-whisper 按名字加载"
                "（需要能访问 HuggingFace；本机通常不可达，请改用 --model-dir）",
                flush=True,
            )

    os.makedirs(args.output, exist_ok=True)
    model = WhisperModel(str(model_ref), device=args.device, compute_type=args.compute_type)
    files = sorted(os.listdir(args.input))
    rows = []
    failures = 0
    for name in tqdm(files):
        path = os.path.join(args.input, name)
        if not os.path.isfile(path):
            continue
        try:
            segments, info = model.transcribe(
                path,
                beam_size=5,
                vad_filter=True,
                language=args.language,
                vad_parameters=dict(min_silence_duration_ms=500),
            )
            text = "".join(s.text for s in segments).strip()
            rows.append(f"{path}|sliced|ZH|{text}")
        except Exception as exc:
            failures += 1
            print(f"{name}: FAIL {exc}")

    out_name = os.path.basename(args.input.rstrip("/\\"))
    out_path = os.path.join(args.output, f"{out_name}_whisper.list")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(rows) + "\n")
    print(f"完成 {len(rows)} 段（失败 {failures}）-> {out_path}")
    if not rows:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
