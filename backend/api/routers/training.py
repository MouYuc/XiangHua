"""Training pipeline controls and dataset activation."""

from __future__ import annotations

import os
import shutil
from functools import partial
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile

from backend.api import deps
from backend.schemas import (
    FeatureRequest,
    ModelRunRequest,
    StopRequest,
    TrainRequest,
    TranscriptUpdate,
)
from backend.services import (
    core,
    voice_assets,
)
from backend.services.training import (
    ST_DONE,
    apply_patch,
    clean_stale_prepare_artifacts,
    cmd_asr,
    cmd_feat,
    cmd_slice,
    cmd_train,
    find_models,
    job_log_path,
    last_job_id,
    list_checkpoint_entries,
    list_metrics,
    mark_step,
    missing_deps,
    patch_status,
    read_list,
    running_jobs,
    scan_exp_checkpoints,
    start_deps_job,
    start_job,
    step_status,
    stop_job,
    tail_log,
    train_env_rows,
    write_corrected,
)
from backend.services.training import (
    load_state as load_train_state,
)
from backend.services.training import (
    save_state as save_train_state,
)

router = APIRouter()


@router.get("/training/status")
def training_status() -> dict[str, Any]:
    config = core.load_config()
    return {"state": load_train_state(), "environment": train_env_rows(config), "stats": core.training_stats(config), "running": running_jobs(), "log": deps._current_log(), "last_job_id": last_job_id()}

@router.get("/training/metrics")
def training_metrics(job_id: str | None = None) -> dict[str, Any]:
    job_id = job_id or last_job_id()
    if not job_id:
        raise HTTPException(status_code=400, detail="还没有训练任务记录。")
    return {"rows": list_metrics(job_id)}

@router.get("/training/checkpoints")
def training_checkpoints() -> dict[str, Any]:
    """训练产物清单：当前实验在权重目录里的真实文件为准（历史记录文件常为空）。"""
    rows = list_checkpoint_entries()
    try:
        cfg = core.load_config()
        root = deps._root_or_400(cfg)
        exp = str(load_train_state().get("exp") or "").strip()
        if exp:
            disk_rows = scan_exp_checkpoints(str(root), exp)
            if disk_rows:
                rows = disk_rows
    except HTTPException:
        pass
    return {"rows": rows}

@router.post("/training/dependencies")
def install_dependencies() -> dict[str, Any]:
    missing = missing_deps()
    if not missing:
        return {"message": "训练依赖已齐全，无需安装。", "job_id": "", "log": deps._current_log()}
    job_id = start_deps_job(missing)
    return {"message": "正在后台安装：" + ", ".join(missing), "job_id": job_id, "log": deps._current_log(job_id)}

@router.post("/training/patch")
def training_patch() -> dict[str, Any]:
    config = core.load_config()
    root = deps._root_or_400(config)
    ok, message = apply_patch(str(root))
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {"message": message, "environment": train_env_rows(config)}

@router.post("/training/slice")
async def training_slice(
    recording: UploadFile = File(...),
    silence_threshold: float = Form(-35),
    min_length: int = Form(4000),
    min_gap: int = Form(300),
    keep_silence: int = Form(500),
) -> dict[str, Any]:
    config = core.load_config()
    root = deps._root_or_400(config)
    if min_length < min_gap or min_gap < 10 or keep_silence < 10:
        raise HTTPException(status_code=400, detail="参数不满足约束：最短段长 >= 最短间隔 >= 10，保留静音 >= 10。")
    filename = Path(deps._upload_filename(recording, "recording.wav")).name
    raw = root / "custom_data" / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    destination = raw / filename
    with destination.open("wb") as output:
        shutil.copyfileobj(recording.file, output)
    job_id = start_job("slice", "录音切片", cmd_slice(str(root), silence_threshold, min_length, min_gap, keep_silence))
    return {"job_id": job_id, **deps._job_message("slice", f"切片任务已启动。录音已放入 {destination}。")}

@router.post("/training/asr")
def training_asr(request: ModelRunRequest) -> dict[str, Any]:
    config = core.load_config()
    root = deps._root_or_400(config)
    if not any((root / "custom_data" / "sliced").glob("*.wav")):
        raise HTTPException(status_code=400, detail="还没有切片数据，请先完成录音切片。")
    job_id = start_job("asr", "Whisper 识别", cmd_asr(str(root), model=request.model))
    return {"job_id": job_id, **deps._job_message("asr", f"识别任务已启动（模型 {request.model}）。")}

@router.get("/training/transcript")
def get_transcript() -> dict[str, Any]:
    root = deps._root_or_400(core.load_config())
    source = deps._transcript_path(root)
    if not source.exists():
        raise HTTPException(status_code=404, detail="还没有识别结果，请先完成 Whisper 识别。")
    sliced_root = (root / "custom_data" / "sliced").resolve()
    # 路径按文件名修复：标注里存的是绝对路径，项目换机器后它们全部失效，
    # 那样既听不到试听、特征提取也必然失败。
    rows = read_list(source, sliced_dir=sliced_root)
    repaired = 0
    for row in rows:
        if row.get("path_repaired"):
            repaired += 1
        try:
            audio_path = Path(str(row.get("path") or "")).resolve(strict=True)
            audio_path.relative_to(sliced_root)
            row["audio_url"] = deps._as_url(core.register_download(audio_path))
        except (OSError, ValueError):
            row["audio_url"] = ""
    return {
        "rows": rows,
        "repaired_paths": repaired,
        "repair_note": (
            f"检测到 {repaired} 条标注的音频路径在本机不存在，已按文件名在切片目录里重新定位。"
            "保存后会写回列表。"
            if repaired
            else ""
        ),
    }

@router.put("/training/transcript")
def save_transcript(request: TranscriptUpdate) -> dict[str, Any]:
    root = deps._root_or_400(core.load_config())
    # 空清单守卫：页面在转写加载完成前点「保存并继续」，会把整个 corrected 清单
    # 覆盖成 0 行——特征提取会"成功"处理零行数据，训练随后在引擎里除零崩溃。
    # Whisper 清单有内容时，拒绝把空清单当真实校对结果保存。
    whisper = deps._transcript_path(root)
    if not request.rows and whisper.is_file() and whisper.read_text(encoding="utf-8").strip():
        raise HTTPException(
            status_code=400,
            detail="转写还没加载（0 句），不能保存空清单。请刷新页面等「校对转写」出现内容，或点「跳过校对直接继续」。",
        )
    output = root / "custom_data" / "asr" / "sliced_corrected.list"
    output.parent.mkdir(parents=True, exist_ok=True)
    result = write_corrected(output, request.rows)
    note = f"已保存 {result['lines']} 句"
    if result["sanitized"]:
        note += f"，其中 {result['sanitized']} 句含竖线已替换为全角"
    mark_step("correct", ST_DONE, note=note)
    return deps._job_message("correct", f"{note}到 sliced_corrected.list，可以进入特征提取。")

@router.post("/training/transcript/skip")
def skip_transcript() -> dict[str, Any]:
    root = deps._root_or_400(core.load_config())
    source = deps._transcript_path(root)
    if not source.exists():
        raise HTTPException(status_code=404, detail="还没有识别结果，请先完成 Whisper 识别。")
    destination = root / "custom_data" / "asr" / "sliced_corrected.list"
    shutil.copy2(source, destination)
    mark_step("correct", ST_DONE, note="直接使用识别结果")
    return deps._job_message("correct", "已直接使用识别结果。广普错字较多，建议仍抽时间校对。")

@router.post("/training/features")
def training_features(request: FeatureRequest) -> dict[str, Any]:
    config = core.load_config()
    root = deps._root_or_400(config)
    experiment = deps._valid_experiment(request.experiment)
    if not (root / "custom_data" / "asr" / "sliced_corrected.list").exists():
        raise HTTPException(status_code=400, detail="请先完成校对或直接使用识别结果。")
    if not patch_status(str(root)):
        raise HTTPException(status_code=400, detail="广普补丁未应用，请先应用补丁。")
    job_id = start_job(
        "feat",
        "特征提取",
        cmd_feat(str(root), experiment),
        pre=partial(clean_stale_prepare_artifacts, str(root)),
    )
    return {"job_id": job_id, **deps._job_message("feat", "特征提取已启动（4 个子步）。")}

@router.post("/training/run")
def training_run(request: TrainRequest) -> dict[str, Any]:
    config = core.load_config()
    root = deps._root_or_400(config)
    experiment = deps._valid_experiment(request.experiment)
    if not (root / "custom_data" / "dataset" / "2-name2text-0.txt").exists():
        raise HTTPException(status_code=400, detail="特征提取未完成，请先完成特征提取。")
    state = load_train_state()
    state.update(exp=experiment, s1_epochs=request.s1_epochs, s2_epochs=request.s2_epochs)
    save_train_state(state)
    commands, pre = cmd_train(str(root), experiment, request.s1_epochs, request.s2_epochs)
    job_id = start_job("train", "模型训练", commands, pre=pre, progress_meta={"s1_epochs": request.s1_epochs, "s2_epochs": request.s2_epochs, "gpu": os.environ.get("CUDA_VISIBLE_DEVICES", "0") or "0"})
    return {"job_id": job_id, **deps._job_message("train", "训练已启动：生成配置 -> s1 -> s2。")}

@router.post("/training/stop")
def training_stop(request: StopRequest) -> dict[str, Any]:
    job_id = request.job_id or last_job_id()
    if not job_id or not stop_job(job_id):
        raise HTTPException(status_code=400, detail="没有可停止的运行中任务。")
    return {"message": f"已请求停止任务 {job_id}", "log": deps._current_log(job_id)}

@router.get("/training/log")
def training_log(job_id: str | None = None, lines: int = Query(default=80, ge=1, le=500)) -> dict[str, str]:
    current = deps._current_log(job_id)
    if job_id and current["job_id"]:
        status = step_status(job_id.split("_")[0])
        path = status.get("log") or job_log_path(job_id)
        current["text"] = tail_log(path, lines) or current["text"]
    return current

@router.post("/training/models/select")
def select_models() -> dict[str, Any]:
    config = core.load_config()
    root = deps._root_or_400(config)
    models = find_models(str(root))
    if not models["gpt"] or not models["sovits"]:
        raise HTTPException(status_code=404, detail="还没找到训练产物，请先完成训练。")
    config.update(gpt_model=models["gpt"], sovits_model=models["sovits"])
    # ⑤ 自动建档（2026-11-21 用户拍板改设计）：训练完成后，同名实验卡存在就追加
    # 版本（版本只增不改），否则以实验名新建声线卡并绑定到引擎配置。建档失败
    # 只提示不阻塞「启用训练模型」主路。
    card_message = ""
    experiment = str(load_train_state().get("exp") or "").strip()
    if experiment:
        try:
            existing = next(
                (
                    card
                    for card in deps.VOICE_CARDS.list_cards()
                    if card.get("display_name") == experiment and card.get("status") != "unknown"
                ),
                None,
            )
            if existing:
                card = deps.VOICE_CARDS.add_version(
                    existing["voice_id"], config, notes="训练产物自动建档"
                )
                card_message = f" 声线卡「{card['display_name']}」已追加版本 v{card['active_version']}。"
            else:
                card = deps.VOICE_CARDS.create(config, experiment, notes="训练产物自动建档")
                card_message = f" 已自动创建声线卡「{card['display_name']}」v{card['active_version']}。"
            config["voice_card_id"] = card["voice_id"]
        except voice_assets.VoiceAssetError as exc:
            card_message = f" （自动建档未成功：{exc}）"
    return {"message": f"已填入并保存最新训练模型，可回合成页试音。{card_message}", "config": core.save_config(config)}
