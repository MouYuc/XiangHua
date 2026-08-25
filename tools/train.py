# -*- coding: utf-8 -*-
"""广普训练后端（WebUI「训练」页专用，纯 Python，不依赖 gradio）。

把 GPT-SoVITS 训练链路（切片 / Whisper 识别 / 人工校对 / 特征提取 / s1+s2 训练）
封装为：
  - 命令构建函数（cmd_*）
  - 后台任务执行器（start_job / stop_job / job_info / tail_log）
  - 训练状态持久化（<仓库>/.guangpu_webui/train_state.json，不入库）
  - 训练产物扫描（find_models）与补丁检查/应用（patch_status / apply_patch）

约定与 docs/TRAINING_GUIDE.md 一致：
  root = GPT-SoVITS 根目录（$env:GPT_SOVITS_ROOT）
  录音 -> <root>/custom_data/raw/，切片 -> <root>/custom_data/sliced/，
  识别 -> <root>/custom_data/asr/，特征 -> <root>/custom_data/dataset/，
  产物 -> <root>/GPT_weights_v2Pro/ 与 <root>/SoVITS_weights_v2Pro/。
"""

import ast
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / ".guangpu_webui"
TRAIN_STATE_FILE = CONFIG_DIR / "train_state.json"
TRAIN_LOGS_DIR = CONFIG_DIR / "train_logs"

# 步骤状态
ST_PENDING = "pending"   # 待办
ST_RUNNING = "running"   # 进行中
ST_DONE = "done"         # 完成
ST_FAIL = "fail"         # 失败（详见日志）

STEPS = ["slice", "asr", "correct", "feat", "train"]
STEP_TITLES = {
    "slice": "录音切片",
    "asr": "Whisper 识别",
    "correct": "人工校对",
    "feat": "特征提取",
    "train": "模型训练",
}

_JOBS = {}          # job_id -> {"proc": Popen|None, "thread": Thread, "log": Path, "step": str, "title": str}
_JOBS_LOCK = threading.Lock()
_LAST_JOB_ID = {"id": None}   # 最近启动的 job，供页面轮询显示日志

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ================= 状态持久化 =================
def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _new_job_id(step):
    return f"{step}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


def default_state():
    return {
        "exp": "myvoice",
        "s1_epochs": 15,
        "s2_epochs": 12,
        "steps": {
            s: {
                "status": ST_PENDING,
                "title": STEP_TITLES[s],
                "log": "",
                "started": "",
                "ended": "",
                "rc": None,
                "progress": default_progress(),
            }
            for s in STEPS
        },
    }


def default_progress(stage="", epochs=0, gpu=""):
    """返回可持久化的训练进度结构，字段保持稳定以兼容旧状态文件。"""
    return {
        "stage": stage,
        "epochs": int(epochs or 0),
        "epoch": 0,
        "batch": 0,
        "batches": 0,
        "batch_percent": 0.0,
        "stage_percent": 0.0,
        "overall_percent": 0.0,
        "global_percent": 0.0,
        "global_step": None,
        "loss_d": None,
        "loss_g": None,
        "learning_rate": None,
        "gpu": gpu,
    }


def _progress_percent(stage, epoch, batch_percent, epochs):
    epochs = max(int(epochs or 0), 1)
    epoch = max(int(epoch or 1), 1)
    stage_percent = min(100.0, max(0.0, ((epoch - 1) + float(batch_percent or 0) / 100.0) / epochs * 100.0))
    if stage == "s2":
        overall_percent = 50.0 + stage_percent / 2.0
    elif stage == "s1":
        overall_percent = stage_percent / 2.0
    else:
        overall_percent = 0.0
    return round(stage_percent, 2), round(overall_percent, 2)


def parse_training_progress(text, stage="", epochs=0, initial=None):
    """解析 GPT-SoVITS s1 Lightning / s2 日志，返回最后一个进度事件。

    s1 常见输出为 ``Epoch 2: 45%|...| 90/200``；s2 输出为
    ``Train Epoch: 4 [3%]``，下一行通常包含 loss、global step 和学习率。
    """
    progress = default_progress(stage=stage, epochs=epochs)
    if isinstance(initial, dict):
        previous_stage = initial.get("stage")
        progress.update(initial)
        progress["stage"] = stage
        progress["epochs"] = int(epochs or progress.get("epochs") or 0)
        if previous_stage != stage:
            for key in ("epoch", "batch", "batches", "batch_percent", "stage_percent", "global_step", "loss_d", "loss_g", "learning_rate"):
                progress[key] = default_progress().get(key)
            if stage == "s2":
                progress["overall_percent"] = 50.0
                progress["global_percent"] = 50.0
    for raw in re.split(r"[\r\n]+", text or ""):
        line = raw.strip()
        if not line:
            continue

        s1_match = re.search(
            r"Epoch\s+(\d+)\s*:.*?(\d{1,3})%\|.*?(\d+)\s*/\s*(\d+)",
            line,
            re.IGNORECASE,
        )
        if s1_match and stage == "s1":
            epoch = int(s1_match.group(1)) + 1
            batch_percent = float(s1_match.group(2))
            batch = int(s1_match.group(3))
            batches = int(s1_match.group(4))
            stage_percent, overall_percent = _progress_percent(stage, epoch, batch_percent, epochs)
            progress.update(
                epoch=epoch,
                batch=batch,
                batches=batches,
                batch_percent=batch_percent,
                stage_percent=stage_percent,
                overall_percent=overall_percent,
                global_percent=overall_percent,
            )
            continue

        s2_match = re.search(r"Train\s+Epoch:\s*(\d+)\s*\[\s*(\d+(?:\.\d+)?)%\s*\]", line, re.IGNORECASE)
        if s2_match and stage == "s2":
            epoch = int(s2_match.group(1))
            batch_percent = float(s2_match.group(2))
            stage_percent, overall_percent = _progress_percent(stage, epoch, batch_percent, epochs)
            progress.update(
                epoch=epoch,
                batch_percent=batch_percent,
                stage_percent=stage_percent,
                overall_percent=overall_percent,
                global_percent=overall_percent,
            )
            continue

        completed_match = re.search(r"====>\s*Epoch:\s*(\d+)", line, re.IGNORECASE)
        if completed_match and stage == "s2":
            epoch = int(completed_match.group(1))
            stage_percent, overall_percent = _progress_percent(stage, epoch, 100.0, epochs)
            progress.update(
                epoch=epoch,
                batch_percent=100.0,
                stage_percent=stage_percent,
                overall_percent=overall_percent,
                global_percent=overall_percent,
            )
            continue

        values_match = re.search(r"(\[[^\r\n]*\])\s*$", line)
        if values_match:
            try:
                values = ast.literal_eval(values_match.group(1))
            except (SyntaxError, ValueError):
                values = None
            if isinstance(values, list) and len(values) >= 2 and all(isinstance(v, (int, float)) for v in values):
                progress["loss_d"] = float(values[0])
                progress["loss_g"] = float(values[1])
                if len(values) >= 3 and isinstance(values[-2], (int, float)):
                    progress["global_step"] = int(values[-2])
                if len(values) >= 4 and isinstance(values[-1], (int, float)):
                    progress["learning_rate"] = float(values[-1])

        gpu_match = re.search(r'''(?:gpu_numbers|CUDA_VISIBLE_DEVICES)["'=: ]+([0-9, -]+)''', line, re.IGNORECASE)
        if gpu_match:
            progress["gpu"] = gpu_match.group(1).strip()

    return progress


def load_state():
    try:
        data = json.loads(TRAIN_STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            st = default_state()
            st.update({k: v for k, v in data.items() if k in st})
            for s in STEPS:
                base = default_state()["steps"][s]
                current = st["steps"].get(s)
                if not isinstance(current, dict):
                    current = {}
                merged = dict(base)
                merged.update(current)
                progress = dict(base["progress"])
                if isinstance(current.get("progress"), dict):
                    progress.update(current["progress"])
                merged["progress"] = progress
                st["steps"][s] = merged
            return st
    except (OSError, ValueError):
        pass
    return default_state()


def save_state(state):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        TRAIN_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _update_step(step, **fields):
    """读-改-写训练状态里某个步骤的字段（线程安全足够：GIL + 小文件）。"""
    state = load_state()
    state["steps"].setdefault(step, default_state()["steps"][step]).update(fields)
    save_state(state)
    return state


def step_status(step):
    """返回某个步骤的当前状态 dict（未知步骤返回 pending 空状态，不抛错）。"""
    st = load_state()["steps"].get(step)
    if st is None:
        return {"status": ST_PENDING, "title": step, "log": "", "started": "", "ended": "", "rc": None}
    return st


def mark_step(step, status, note=""):
    """手动标记某步骤状态（用于无后台任务的步骤，如「人工校对」完成）。"""
    _update_step(step, status=status, note=note, ended=_now())


# ================= 后台任务执行器 =================
def _stage_for_command(cmd):
    command = " ".join(str(part) for part in cmd)
    if "s1_train.py" in command:
        return "s1"
    if "s2_train.py" in command:
        return "s2"
    if "build_train_configs.py" in command:
        return "prepare"
    return ""


def _stage_epochs(stage, progress_meta):
    if stage == "s1":
        return int((progress_meta or {}).get("s1_epochs") or 0)
    if stage == "s2":
        return int((progress_meta or {}).get("s2_epochs") or 0)
    return 0


def _record_progress(step, stage, text, progress_meta):
    if step != "train" or not stage:
        return
    state = load_state()
    current = state["steps"].get(step, {}).get("progress") or default_progress()
    epochs = _stage_epochs(stage, progress_meta)
    parsed = parse_training_progress(text, stage=stage, epochs=epochs, initial=current)
    if progress_meta and progress_meta.get("gpu"):
        parsed["gpu"] = progress_meta["gpu"]
    if parsed != current or current.get("stage") != stage:
        _update_step(step, progress=parsed, current_stage=stage)


def start_job(step, title, cmds, pre=None, progress_meta=None):
    """启动一个后台训练任务。

    cmds: 依次执行的命令列表，每项为 (cmd_list, cwd, env|None)；
          env 为 None 时继承当前环境。任一步失败即中止，状态置 fail。
    pre:  可选零参 callable，在线程开始、写日志前调用（如建目录）。
    返回 job_id；日志写入 <CONFIG_DIR>/train_logs/<job_id>.log。
    """
    job_id = _new_job_id(step)
    log = TRAIN_LOGS_DIR / f"{job_id}.log"
    with _JOBS_LOCK:
        _JOBS[job_id] = {"proc": None, "thread": None, "log": log, "step": step, "title": title}
        _LAST_JOB_ID["id"] = job_id

    _update_step(
        step,
        status=ST_RUNNING,
        title=title,
        log=str(log),
        started=_now(),
        ended="",
        rc=None,
        progress=default_progress(gpu=(progress_meta or {}).get("gpu", "")),
    )

    def _run():
        rc = 0
        try:
            TRAIN_LOGS_DIR.mkdir(parents=True, exist_ok=True)
            with open(log, "wb") as f:
                if pre is not None:
                    try:
                        pre()
                    except Exception as exc:
                        f.write(f"[准备失败] {exc}\n".encode("utf-8", "replace"))
                        rc = 1
                if rc == 0:
                    for cmd, cwd, env in cmds:
                        stage = _stage_for_command(cmd)
                        if stage:
                            epochs = _stage_epochs(stage, progress_meta)
                            _update_step(
                                step,
                                current_stage=stage,
                                progress=parse_training_progress(
                                    "", stage=stage, epochs=epochs,
                                    initial=load_state()["steps"].get(step, {}).get("progress"),
                                ),
                            )
                        f.write(("\n$ " + " ".join(cmd) + f"  (cwd={cwd})\n").encode("utf-8", "replace"))
                        f.flush()
                        try:
                            proc = subprocess.Popen(
                                cmd,
                                cwd=cwd,
                                env=env,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                creationflags=_CREATE_NO_WINDOW,
                            )
                        except Exception as exc:
                            f.write(f"[启动失败] {exc}\n".encode("utf-8", "replace"))
                            rc = 1
                            break
                        with _JOBS_LOCK:
                            _JOBS[job_id]["proc"] = proc
                        # 实时转发子进程输出，并消费 \r 进度行（Lightning tqdm 常用 \r 刷新）。
                        try:
                            pending = b""
                            while True:
                                reader = getattr(proc.stdout, "read1", proc.stdout.read)
                                chunk = reader(4096)
                                if not chunk:
                                    break
                                f.write(chunk)
                                f.flush()
                                sys.stdout.buffer.write("  [训练] ".encode("utf-8") + chunk)
                                sys.stdout.buffer.flush()
                                pending += chunk
                                parts = re.split(br"[\r\n]", pending)
                                pending = parts.pop()
                                for raw_line in parts:
                                    _record_progress(step, stage, raw_line.decode("utf-8", "replace"), progress_meta)
                            if pending:
                                _record_progress(step, stage, pending.decode("utf-8", "replace"), progress_meta)
                        except Exception:
                            pass
                        rc = proc.wait()
                        if rc != 0:
                            f.write(f"\n[退出码 {rc}]\n".encode("utf-8", "replace"))
                            break
        finally:
            with _JOBS_LOCK:
                if _JOBS.get(job_id):
                    _JOBS[job_id]["proc"] = None
            fields = {"status": ST_DONE if rc == 0 else ST_FAIL, "rc": rc, "ended": _now()}
            if rc == 0 and step == "train":
                progress = load_state()["steps"].get(step, {}).get("progress") or default_progress()
                progress.update(overall_percent=100.0, global_percent=100.0, stage_percent=100.0)
                fields["progress"] = progress
            _update_step(step, **fields)

    t = threading.Thread(target=_run, daemon=True, name=f"train-{job_id}")
    with _JOBS_LOCK:
        _JOBS[job_id]["thread"] = t
    t.start()
    return job_id


def stop_job(job_id):
    """请求终止一个正在运行的任务；进程被终止后状态会置 fail（rc != 0）。"""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        proc = job["proc"] if job else None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            return True
        except OSError:
            return False
    return False


def running_jobs():
    """返回正在运行的任务列表：[{job_id, step, title, log}...]"""
    out = []
    with _JOBS_LOCK:
        for job_id, job in _JOBS.items():
            proc = job.get("proc")
            if proc is not None and proc.poll() is None:
                out.append({"job_id": job_id, "step": job["step"], "title": job["title"], "log": str(job["log"])})
    return out


def last_job_id():
    return _LAST_JOB_ID["id"]


def job_log_path(job_id):
    """返回某个 job 的日志文件路径（找不到返回空串）。"""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        return str(job["log"]) if job else ""


def start_deps_job(pkgs):
    """安装训练依赖（独立于训练步骤状态机，日志写入 train_logs/deps_*.log）。

    返回 job_id；完成后日志以「[退出码 N]」结尾，页面可据此判断成败。
    """
    job_id = _new_job_id("deps")
    log = TRAIN_LOGS_DIR / f"{job_id}.log"
    with _JOBS_LOCK:
        _JOBS[job_id] = {"proc": None, "thread": None, "log": log, "step": "deps", "title": "安装训练依赖"}
        _LAST_JOB_ID["id"] = job_id

    def _run():
        try:
            TRAIN_LOGS_DIR.mkdir(parents=True, exist_ok=True)
            with open(log, "wb") as f:
                f.write((f"$ {sys.executable} -m pip install {' '.join(pkgs)}\n").encode("utf-8", "replace"))
                f.flush()
                try:
                    proc = subprocess.Popen(
                        [sys.executable, "-m", "pip", "install", *pkgs],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        creationflags=_CREATE_NO_WINDOW,
                    )
                except Exception as exc:
                    f.write(f"[启动失败] {exc}\n".encode("utf-8", "replace"))
                    return
                with _JOBS_LOCK:
                    _JOBS[job_id]["proc"] = proc
                # 实时转发 pip 输出：同时写入日志文件（页面日志区）与 WebUI 启动终端
                try:
                    while True:
                        line = proc.stdout.readline()
                        if not line:
                            break
                        f.write(line)
                        f.flush()
                        sys.stdout.buffer.write("  [依赖] ".encode("utf-8") + line)
                        sys.stdout.buffer.flush()
                except Exception:
                    pass
                rc = proc.wait()
                f.write(f"\n[退出码 {rc}]\n".encode("utf-8", "replace"))
        finally:
            with _JOBS_LOCK:
                if _JOBS.get(job_id):
                    _JOBS[job_id]["proc"] = None

    t = threading.Thread(target=_run, daemon=True, name=f"deps-{job_id}")
    with _JOBS_LOCK:
        _JOBS[job_id]["thread"] = t
    t.start()
    return job_id


def tail_log(log_path, n=80):
    """读取日志尾部 n 行（含正在写入时的部分行）。失败返回空串。"""
    try:
        p = Path(log_path)
        size = p.stat().st_size
        if size == 0:
            return ""
        # 只读末尾 128KB，避免每次轮询把整个日志读进内存（训练日志可能很大）
        with p.open("rb") as f:
            f.seek(max(0, size - 131072))
            data = f.read()
        lines = data.decode("utf-8", "replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return ""


# ================= 命令构建 =================
def cmd_slice(root, sil=-35, min_len=4000, min_gap=300, keep_sil=500, peak=0.9, mix=0.25):
    """① 长录音 -> 一句一句的切片（在 GPT-SoVITS 目录运行 tools/slice_audio.py）。"""
    return [
        (
            [
                sys.executable, "-s", "tools/slice_audio.py",
                "custom_data/raw", "custom_data/sliced",
                str(sil), str(min_len), str(min_gap), "10",
                str(keep_sil), str(peak), str(mix), "0", "1",
            ],
            root,
            None,
        )
    ]


def cmd_asr(root, model="large-v3", language="zh"):
    """② 切片 -> Whisper 识别标注（在仓库根目录运行 scripts/run_whisper.py）。"""
    sliced = Path(root) / "custom_data" / "sliced"
    asr = Path(root) / "custom_data" / "asr"
    return [
        (
            [
                sys.executable, str(REPO_ROOT / "scripts" / "run_whisper.py"),
                "--input", str(sliced),
                "--output", str(asr),
                "--model", model,
                "--language", language,
            ],
            str(REPO_ROOT),
            None,
        )
    ]


def cmd_feat(root, exp, corrected_rel="custom_data/asr/sliced_corrected.list"):
    """④ 特征提取：依次跑 1-get-text / 2-get-hubert / 2-get-sv / 3-get-semantic。

    环境变量按 docs/TRAINING_GUIDE.md 第 4 步的推荐值构造（相对路径，cwd=root）。
    """
    base = {
        "inp_text": corrected_rel,
        "inp_wav_dir": "custom_data/sliced",
        "exp_name": exp,
        "i_part": "0",
        "all_parts": "1",
        "opt_dir": "custom_data/dataset",
        "bert_pretrained_dir": "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large",
        "version": "v2Pro",
    }
    py = [sys.executable, "-s", "GPT_SoVITS/prepare_datasets/"]

    def env(**extra):
        e = dict(os.environ)
        e.update(base)
        e.update(extra)
        return e

    return [
        (py + ["1-get-text.py"], root, env()),
        (py + ["2-get-hubert-wav32k.py"], root, env(cnhubert_base_dir="GPT_SoVITS/pretrained_models/chinese-hubert-base")),
        (py + ["2-get-sv.py"], root, env(sv_path="GPT_SoVITS/pretrained_models/sv/pretrained_eres2netv2w24s4ep4.ckpt")),
        (
            py + ["3-get-semantic.py"],
            root,
            env(
                pretrained_s2G="GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth",
                s2config_path="GPT_SoVITS/configs/s2v2Pro.json",
            ),
        ),
    ]


def cmd_build_configs(root, exp, s1_epochs=15, s2_epochs=12):
    """⑤a 生成 s1/s2 训练配置（在仓库根目录运行 scripts/build_train_configs.py）。"""
    dataset = str(Path(root) / "custom_data" / "dataset")
    return [
        (
            [
                sys.executable, str(REPO_ROOT / "scripts" / "build_train_configs.py"),
                "--root", str(root),
                "--dataset", dataset,
                "--exp", exp,
                "--s1-epochs", str(s1_epochs),
                "--s2-epochs", str(s2_epochs),
                "--version", "v2Pro",
            ],
            str(REPO_ROOT),
            None,
        )
    ]


def _ensure_s2_logs(root):
    """s2 训练前必须存在 logs_s2_v2Pro 目录（脚本不会自动创建）。"""
    (Path(root) / "custom_data" / "dataset" / "logs_s2_v2Pro").mkdir(parents=True, exist_ok=True)


def cmd_train(root, exp, s1_epochs=15, s2_epochs=12):
    """⑤b 训练：生成配置 -> 建 s2 日志目录 -> s1 -> s2（一个任务串行完成）。"""
    cmds = cmd_build_configs(root, exp, s1_epochs, s2_epochs)
    cmds += [
        ([sys.executable, "-s", "GPT_SoVITS/s1_train.py", "--config_file", "TEMP/tmp_s1.yaml"], root, None),
        ([sys.executable, "-s", "GPT_SoVITS/s2_train.py", "--config", "TEMP/tmp_s2.json"], root, None),
    ]
    return cmds, _ensure_s2_logs


# ================= 训练产物扫描 =================
def find_models(root):
    """扫描训练产物，返回最新 {gpt, sovits} 模型绝对路径（找不到为空串）。"""
    def latest(d, exts):
        try:
            cands = [f for f in Path(d).iterdir() if f.suffix in exts]
        except OSError:
            return ""
        if not cands:
            return ""
        return str(max(cands, key=lambda f: f.stat().st_mtime))

    root = Path(root)
    return {
        "gpt": latest(root / "GPT_weights_v2Pro", {".ckpt"}),
        "sovits": latest(root / "SoVITS_weights_v2Pro", {".pth"}),
    }


# ================= 标注文件读写（校对用） =================
def read_list(path):
    """读取 .list 标注文件 -> [{"path": ..., "text": ...}]。"""
    rows = []
    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        parts = ln.split("|")
        rows.append({"path": parts[0], "text": "|".join(parts[3:]) if len(parts) > 3 else ""})
    return rows


def write_corrected(path, rows):
    """把校对后的 rows 写回 .list（保留 |sliced|ZH| 段）。"""
    lines = [f"{r['path']}|sliced|ZH|{r['text']}" for r in rows if r.get("path")]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


# ================= 补丁检查 / 应用 =================
def patch_status(root):
    """广普发音补丁是否已应用到 GPT-SoVITS（chinese.py / chinese2.py 含 GP_PINYIN）。"""
    try:
        for name in ("chinese.py", "chinese2.py"):
            p = Path(root) / "GPT_SoVITS" / "text" / name
            if not p.exists() or "GP_PINYIN" not in p.read_text(encoding="utf-8", errors="ignore"):
                return False
        return True
    except OSError:
        return False


def apply_patch(root):
    """应用 patches/pinyin.patch 到 GPT-SoVITS 根目录。返回 (ok, message)。"""
    patch = REPO_ROOT / "patches" / "pinyin.patch"
    if not patch.exists():
        return False, f"补丁文件不存在：{patch}"
    if patch_status(root):
        return True, "补丁已应用，无需重复操作。"
    try:
        r = subprocess.run(["git", "apply", str(patch)], cwd=root, capture_output=True, text=True, errors="replace")
    except Exception as exc:
        return False, f"执行 git apply 失败：{exc}"
    if r.returncode == 0:
        return True, "补丁应用成功。"
    msg = (r.stderr or r.stdout or "").strip().splitlines()
    return False, "补丁应用失败：" + (msg[-1] if msg else str(r.returncode))


# ================= 依赖检查（训练页专用） =================
def missing_deps():
    """返回当前环境缺失的训练依赖列表（faster_whisper / yaml）。"""
    import importlib.util

    return [m for m in ("faster_whisper", "yaml") if importlib.util.find_spec(m) is None]


def train_env_rows(cfg):
    """训练页环境检查行：[{name, status(ok|bad|warn), detail, fix}...]"""
    import importlib.util

    rows = []

    def add(name, status, detail, fix=""):
        rows.append({"name": name, "status": status, "detail": detail, "fix": fix})

    root = cfg.get("gpt_sovits_root", "")
    root_ok = os.path.isdir(root) and os.path.isdir(os.path.join(root, "GPT_SoVITS"))
    add("GPT-SoVITS 根目录", "ok" if root_ok else "bad", root or "未配置",
        "在「设置」页配置 GPT_SOVITS_ROOT" if not root_ok else "")

    if root_ok:
        patched = patch_status(root)
        add("广普发音补丁", "ok" if patched else "bad",
            "已应用" if patched else "未应用（训练前必须打）", "点击下方「一键打补丁」")

    cuda = False
    try:
        import torch
        cuda = torch.cuda.is_available()
        add("CUDA 可用", "ok" if cuda else "bad", "可用" if cuda else "不可用（训练必须 CUDA）",
            "按 GPT-SoVITS 环境说明安装 CUDA 版 torch" if not cuda else "")
    except Exception as exc:
        add("PyTorch", "bad", str(exc), "pip install torch==2.6.0+cu126")

    missing = missing_deps()
    add("训练依赖 faster-whisper / yaml", "ok" if not missing else "bad",
        "已安装" if not missing else "缺少：" + ", ".join(missing),
        "点「一键安装训练依赖」，或 pip install faster-whisper pyyaml" if missing else "")

    data = Path(root) / "custom_data"
    sliced = len(list((data / "sliced").glob("*.wav"))) if (data / "sliced").is_dir() else 0
    add("切片数据", "ok" if sliced else "warn", f"{sliced} 段", "先完成第 1 步切片" if not sliced else "")
    return rows


def train_env_html(cfg):
    rows = train_env_rows(cfg)
    dots = "".join(
        '<div class="gp-train-env-row" style="display:flex;justify-content:space-between;align-items:center;gap:12px;padding:9px 0;'
        f'border-bottom:1px solid var(--gp-border);"><span style="color:var(--gp-ink);font-size:13.5px;">{r["name"]}</span>'
        f'<span style="color:var(--gp-{r["status"]});font-size:12.5px;">'
        f'<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--gp-{r["status"]});margin-right:6px;"></span>'
        f'{r["detail"]}</span></div>'
        for r in rows
    )
    fixes = [r["fix"] for r in rows if r["fix"] and r["status"] != "ok"]
    if fixes:
        code = "".join(f'<div style="padding:3px 0;">{f}</div>' for f in fixes)
        fix_html = ('<div class="gp-train-env-fix" style="background:var(--gp-soft);border:1px solid var(--gp-border);border-radius:10px;'
                    f'padding:12px 14px;margin-top:14px;font-family:var(--gp-mono);font-size:12px;color:var(--gp-grn);">{code}</div>')
    else:
        fix_html = '<div class="gp-train-env-fix" style="color:var(--gp-ok);margin-top:14px;font-size:13px;">✓ 环境就绪，可以开始训练。</div>'
    return '<div class="gp-train-env-check">' + dots + fix_html + '</div>'
