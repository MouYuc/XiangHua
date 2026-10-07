"""FastAPI application for the Vue XiangHua WebUI."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
import json

from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend.schemas import (
    ConfigUpdate,
    DoctorRequest,
    EnvironmentSelectRequest,
    FeatureRequest,
    ModelRunRequest,
    StopRequest,
    SynthesisRequest,
    TimbreConvertRequest,
    TimbreProfileRequest,
    TrainRequest,
    TranscriptUpdate,
)
from backend.services import core, timbre
from backend.services.doctor import run_doctor
from backend.services.synthesis_jobs import SYNTHESIS_JOBS
from backend.services.timbre import TIMBRE_BACKEND
from backend.services.voice_selection import apply_voice_selection, list_voice_options
from tools.env import derive_environment_config, scan_gpt_sovits_roots
from tools.train import (
    ST_DONE,
    apply_patch,
    cmd_asr,
    cmd_feat,
    cmd_slice,
    cmd_train,
    find_models,
    job_log_path,
    last_job_id,
    list_checkpoint_entries,
    list_metrics,
    load_state as load_train_state,
    mark_step,
    missing_deps,
    patch_status,
    read_list,
    running_jobs,
    save_state as save_train_state,
    start_deps_job,
    start_job,
    step_status,
    stop_job,
    tail_log,
    train_env_rows,
    write_corrected,
)


def _as_url(download_id: str) -> str:
    return f"/api/files/{download_id}" if download_id else ""


def _merge_config(update: ConfigUpdate) -> dict[str, Any]:
    config = core.load_config()
    payload = update.model_dump(exclude_none=True, exclude={"text", "output_dir", "zip_output"})
    # Optional mixed controls use null as an explicit request to restore dynamic defaults.
    for field in ("mixed_eos_floor", "mixed_token_cap"):
        if field in update.model_fields_set:
            payload[field] = getattr(update, field)
    config.update(payload)
    return config


def _root_or_400(config: dict[str, Any]) -> Path:
    root = Path(str(config.get("gpt_sovits_root") or "")).expanduser()
    if not (root / "GPT_SoVITS").is_dir():
        raise HTTPException(status_code=400, detail="GPT-SoVITS 根目录不可用，请先在设置页配置。")
    return root


def _job_message(step: str, message: str) -> dict[str, Any]:
    return {"message": message, "step": step_status(step), "log": _current_log()}


def _public_synthesis_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(snapshot or {})
    outputs: list[dict[str, Any]] = []
    for item in data.get("outputs") or []:
        row = dict(item)
        row.pop("file", None)
        row["url"] = _as_url(str(row.get("download_id") or ""))
        outputs.append(row)
    data["outputs"] = outputs
    return data


def _public_synthesis_event(event: dict[str, Any]) -> dict[str, Any]:
    data = dict(event)
    if data.get("type") == "snapshot":
        data["snapshot"] = _public_synthesis_snapshot(data.get("snapshot"))
    return data


def _public_timbre_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Attach a playable URL to a finished conversion result.

    The result file lives under ``output/vc/webui``; registering it as a download
    is what lets the browser play it without exposing the filesystem path.
    """
    data = dict(snapshot or {})
    result = data.get("result")
    if isinstance(result, dict) and result.get("file"):
        row = dict(result)
        path = Path(str(row["file"]))
        row.pop("file", None)
        row["url"] = _as_url(core.register_download(path)) if path.is_file() else ""
        data["result"] = row
    return data


def _current_log(job_id: str | None = None) -> dict[str, str]:
    job_id = job_id or last_job_id()
    if not job_id:
        return {"job_id": "", "text": "（还没有任务，日志将显示在这里）"}
    status = step_status(job_id.split("_")[0])
    log = status.get("log") or job_log_path(job_id)
    return {"job_id": job_id, "text": tail_log(log, 80) or f"（任务 {job_id} 刚启动，日志暂为空）"}


def _transcript_path(root: Path) -> Path:
    return root / "custom_data" / "asr" / "sliced_whisper.list"


def _valid_experiment(value: str) -> str:
    name = (value or "myvoice").strip() or "myvoice"
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise HTTPException(status_code=400, detail="实验名只能包含字母、数字、下划线或连字符。")
    return name


def create_app() -> FastAPI:
    app = FastAPI(title="XiangHua API", version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    def start_synthesis_warmup() -> None:
        core.SYNTHESIS_BACKEND.start_warmup(core.load_config())

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/synthesis/readiness")
    def synthesis_readiness() -> dict[str, str]:
        return core.SYNTHESIS_BACKEND.warmup_status()

    @app.post("/api/synthesis/readiness/retry")
    def retry_synthesis_warmup() -> dict[str, str]:
        return core.SYNTHESIS_BACKEND.start_warmup(core.load_config())

    @app.get("/api/config")
    def get_config() -> dict[str, Any]:
        return core.load_config()

    @app.put("/api/config")
    def update_config(request: ConfigUpdate) -> dict[str, Any]:
        return core.save_config(_merge_config(request))

    @app.get("/api/voice/options")
    def voice_options() -> dict[str, Any]:
        return list_voice_options(core.load_config())


    @app.get("/api/overview")
    def overview() -> dict[str, Any]:
        config = core.load_config()
        return {
            "config": config,
            "environment": core.environment_check(config),
            "training": load_train_state(),
            "training_stats": core.training_stats(config),
            "history": core.load_history()[:5],
        }

    @app.get("/api/environment")
    def environment() -> dict[str, Any]:
        config = core.load_config()
        return {"config": config, "rows": core.environment_check(config), "candidates": scan_gpt_sovits_roots(core.REPO_ROOT)}

    @app.post("/api/environment/scan")
    def scan_environment() -> dict[str, Any]:
        config = core.load_config()
        candidates = scan_gpt_sovits_roots(core.REPO_ROOT)
        selected = str(config.get("gpt_sovits_root") or "")
        if not any(os.path.normcase(item["path"]) == os.path.normcase(selected) for item in candidates) and candidates:
            selected = candidates[0]["path"]
            config.update(derive_environment_config(selected, config))
            config = core.save_config(config)
        return {"config": config, "rows": core.environment_check(config), "candidates": candidates, "selected": selected}

    @app.post("/api/environment/select")
    def select_environment(request: EnvironmentSelectRequest) -> dict[str, Any]:
        config = core.load_config()
        selected = Path(request.path).expanduser()
        if not (selected / "GPT_SoVITS").is_dir():
            raise HTTPException(status_code=400, detail="选择的 GPT-SoVITS 环境不存在或不完整。")
        config.update(derive_environment_config(str(selected), config))
        config = core.save_config(config)
        return {"config": config, "rows": core.environment_check(config), "selected": str(selected)}

    @app.post("/api/doctor")
    def doctor(request: DoctorRequest) -> dict[str, Any]:
        return run_doctor(repair=request.repair)

    @app.post("/api/reference-audio")
    def upload_reference_audio(audio: UploadFile = File(...)) -> dict[str, str]:
        try:
            path = core.save_reference_audio(audio.filename or "", audio.file)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail="参考音频保存失败。") from exc
        return {"path": str(path)}

    @app.post("/api/engine/pronunciation-patch")
    def import_pronunciation_patch(patch: UploadFile = File(...)) -> dict[str, str]:
        root = core.load_config()["gpt_sovits_root"]
        if not (Path(root) / "GPT_SoVITS").is_dir():
            raise HTTPException(status_code=400, detail="GPT-SoVITS 根目录不可用。")
        try:
            message = core.apply_pronunciation_patch(root, patch.filename or "", patch.file)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail="发音补丁导入失败。") from exc
        return {"message": message}

    @app.post("/api/synthesis")
    def synthesis(request: SynthesisRequest) -> dict[str, Any]:
        readiness = core.SYNTHESIS_BACKEND.warmup_status()
        if readiness["status"] in {"warming", "failed"}:
            raise HTTPException(status_code=409, detail=readiness["message"])
        config = _merge_config(request)
        try:
            prepared = apply_voice_selection(
                config,
                request.text,
                request.voice_profile_id,
                request.accent_id,
            )
            return _public_synthesis_snapshot(
                SYNTHESIS_JOBS.start(prepared.config, prepared.text)
            )
        except (RuntimeError, ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/synthesis/status/{job_id}")
    def synthesis_status(job_id: str) -> dict[str, Any]:
        snapshot = SYNTHESIS_JOBS.snapshot(job_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="合成任务不存在。")
        return _public_synthesis_snapshot(snapshot)

    @app.get("/api/synthesis/events/{job_id}")
    async def synthesis_events(job_id: str) -> StreamingResponse:
        if SYNTHESIS_JOBS.snapshot(job_id) is None:
            raise HTTPException(status_code=404, detail="合成任务不存在。")

        async def stream():
            async for event in SYNTHESIS_JOBS.stream(job_id):
                yield f"data: {json.dumps(_public_synthesis_event(event), ensure_ascii=False)}\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    @app.get("/api/timbre/profile")
    def timbre_profile(profile_id: str = Query(default="current")) -> dict[str, Any]:
        """Report a voice profile's reference corpus, cache state and VRAM."""
        return TIMBRE_BACKEND.profile_status(profile_id, core.load_config())

    @app.post("/api/timbre/profile/build")
    def timbre_profile_build(request: TimbreProfileRequest) -> dict[str, Any]:
        """Encode a profile's reference audio into a cached kNN matching pool."""
        config = core.load_config()
        try:
            return _public_timbre_snapshot(
                TIMBRE_BACKEND.start_profile_build(request.profile_id, config, device=request.device)
            )
        except (RuntimeError, ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/timbre/sources")
    def timbre_sources(limit: int = Query(default=30, ge=1, le=200)) -> dict[str, Any]:
        """List audio the user can convert: synthesis history plus past uploads."""
        return {"rows": timbre.list_sources(limit)}

    @app.post("/api/timbre/convert")
    async def timbre_convert_upload(
        audio: UploadFile = File(...),
        topk: int = Form(4),
        profile_id: str = Form("current"),
        device: str | None = Form(None),
    ) -> dict[str, Any]:
        """Convert an uploaded recording (speech or singing) into a profile's voice."""
        if topk < 1:
            raise HTTPException(status_code=400, detail="topk 必须 >= 1。")
        try:
            source = timbre.save_source(audio.filename or "source.wav", audio.file)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail="源音频保存失败。") from exc
        config = core.load_config()
        return _public_timbre_snapshot(
            TIMBRE_BACKEND.start_conversion(source, profile_id, config, topk=topk, device=device)
        )

    @app.post("/api/timbre/convert-file")
    def timbre_convert_file(request: TimbreConvertRequest) -> dict[str, Any]:
        """Convert a file already on disk, e.g. a synthesis result just produced."""
        source: Path | None = None
        if request.download_id:
            source = core.resolve_download(request.download_id)
        if source is None and request.source_path:
            candidate = Path(request.source_path).expanduser()
            source = candidate if candidate.is_file() else None
        if source is None or not source.is_file():
            raise HTTPException(status_code=404, detail="找不到要转换的源音频。")
        config = core.load_config()
        try:
            return _public_timbre_snapshot(
                TIMBRE_BACKEND.start_conversion(
                    source, request.profile_id, config, topk=request.topk, device=request.device
                )
            )
        except (RuntimeError, ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/timbre/status/{job_id}")
    def timbre_status(job_id: str) -> dict[str, Any]:
        snapshot = TIMBRE_BACKEND.snapshot(job_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="音色任务不存在。")
        return _public_timbre_snapshot(snapshot)

    @app.get("/api/timbre/jobs")
    def timbre_jobs(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
        return {"rows": [_public_timbre_snapshot(row) for row in TIMBRE_BACKEND.list_jobs(limit)]}

    @app.post("/api/timbre/unload")
    def timbre_unload() -> dict[str, Any]:
        """Release the encoder so synthesis can reclaim the GPU."""
        return TIMBRE_BACKEND.unload()

    @app.get("/api/history")
    def history() -> list[dict[str, Any]]:
        entries = core.load_history_with_downloads()
        for entry in entries:
            entry.pop("file", None)
            entry["url"] = _as_url(str(entry.get("download_id") or ""))
        return entries

    @app.delete("/api/history", status_code=204)
    def delete_history() -> Response:
        core.clear_history()
        return Response(status_code=204)

    @app.get("/api/files")
    def deprecated_file_query() -> None:
        raise HTTPException(status_code=404, detail="请使用下载链接。")

    @app.get("/api/files/{download_id}")
    def serve_file(download_id: str) -> FileResponse:
        candidate = core.resolve_download(download_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="下载文件不存在或已不可用。")
        return FileResponse(candidate, filename=candidate.name)

    @app.get("/api/training/status")
    def training_status() -> dict[str, Any]:
        config = core.load_config()
        return {"state": load_train_state(), "environment": train_env_rows(config), "stats": core.training_stats(config), "running": running_jobs(), "log": _current_log(), "last_job_id": last_job_id()}

    @app.get("/api/training/metrics")
    def training_metrics(job_id: str | None = None) -> dict[str, Any]:
        job_id = job_id or last_job_id()
        if not job_id:
            raise HTTPException(status_code=400, detail="还没有训练任务记录。")
        return {"rows": list_metrics(job_id)}

    @app.get("/api/training/checkpoints")
    def training_checkpoints() -> dict[str, Any]:
        return {"rows": list_checkpoint_entries()}

    @app.post("/api/training/dependencies")
    def install_dependencies() -> dict[str, Any]:
        missing = missing_deps()
        if not missing:
            return {"message": "训练依赖已齐全，无需安装。", "job_id": "", "log": _current_log()}
        job_id = start_deps_job(missing)
        return {"message": "正在后台安装：" + ", ".join(missing), "job_id": job_id, "log": _current_log(job_id)}

    @app.post("/api/training/patch")
    def training_patch() -> dict[str, Any]:
        config = core.load_config()
        root = _root_or_400(config)
        ok, message = apply_patch(str(root))
        if not ok:
            raise HTTPException(status_code=400, detail=message)
        return {"message": message, "environment": train_env_rows(config)}

    @app.post("/api/training/slice")
    async def training_slice(
        recording: UploadFile = File(...),
        silence_threshold: float = Form(-35),
        min_length: int = Form(4000),
        min_gap: int = Form(300),
        keep_silence: int = Form(500),
    ) -> dict[str, Any]:
        config = core.load_config()
        root = _root_or_400(config)
        if min_length < min_gap or min_gap < 10 or keep_silence < 10:
            raise HTTPException(status_code=400, detail="参数不满足约束：最短段长 >= 最短间隔 >= 10，保留静音 >= 10。")
        filename = Path(recording.filename or "recording.wav").name
        raw = root / "custom_data" / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        destination = raw / filename
        with destination.open("wb") as output:
            shutil.copyfileobj(recording.file, output)
        job_id = start_job("slice", "录音切片", cmd_slice(str(root), silence_threshold, min_length, min_gap, keep_silence))
        return {"job_id": job_id, **_job_message("slice", f"切片任务已启动。录音已放入 {destination}。")}

    @app.post("/api/training/asr")
    def training_asr(request: ModelRunRequest) -> dict[str, Any]:
        config = core.load_config()
        root = _root_or_400(config)
        if not any((root / "custom_data" / "sliced").glob("*.wav")):
            raise HTTPException(status_code=400, detail="还没有切片数据，请先完成录音切片。")
        job_id = start_job("asr", "Whisper 识别", cmd_asr(str(root), model=request.model))
        return {"job_id": job_id, **_job_message("asr", f"识别任务已启动（模型 {request.model}）。")}

    @app.get("/api/training/transcript")
    def get_transcript() -> dict[str, Any]:
        root = _root_or_400(core.load_config())
        source = _transcript_path(root)
        if not source.exists():
            raise HTTPException(status_code=404, detail="还没有识别结果，请先完成 Whisper 识别。")
        rows = read_list(source)
        sliced_root = (root / "custom_data" / "sliced").resolve()
        for row in rows:
            try:
                audio_path = Path(str(row.get("path") or "")).resolve(strict=True)
                audio_path.relative_to(sliced_root)
                row["audio_url"] = _as_url(core.register_download(audio_path))
            except (OSError, ValueError):
                row["audio_url"] = ""
        return {"rows": rows}

    @app.put("/api/training/transcript")
    def save_transcript(request: TranscriptUpdate) -> dict[str, Any]:
        root = _root_or_400(core.load_config())
        output = root / "custom_data" / "asr" / "sliced_corrected.list"
        output.parent.mkdir(parents=True, exist_ok=True)
        count = write_corrected(output, request.rows)
        mark_step("correct", ST_DONE, note=f"已保存 {count} 句")
        return _job_message("correct", f"已保存 {count} 句到 sliced_corrected.list，可以进入特征提取。")

    @app.post("/api/training/transcript/skip")
    def skip_transcript() -> dict[str, Any]:
        root = _root_or_400(core.load_config())
        source = _transcript_path(root)
        if not source.exists():
            raise HTTPException(status_code=404, detail="还没有识别结果，请先完成 Whisper 识别。")
        destination = root / "custom_data" / "asr" / "sliced_corrected.list"
        shutil.copy2(source, destination)
        mark_step("correct", ST_DONE, note="直接使用识别结果")
        return _job_message("correct", "已直接使用识别结果。广普错字较多，建议仍抽时间校对。")

    @app.post("/api/training/features")
    def training_features(request: FeatureRequest) -> dict[str, Any]:
        config = core.load_config()
        root = _root_or_400(config)
        experiment = _valid_experiment(request.experiment)
        if not (root / "custom_data" / "asr" / "sliced_corrected.list").exists():
            raise HTTPException(status_code=400, detail="请先完成校对或直接使用识别结果。")
        if not patch_status(str(root)):
            raise HTTPException(status_code=400, detail="广普补丁未应用，请先应用补丁。")
        job_id = start_job("feat", "特征提取", cmd_feat(str(root), experiment))
        return {"job_id": job_id, **_job_message("feat", "特征提取已启动（4 个子步）。")}

    @app.post("/api/training/run")
    def training_run(request: TrainRequest) -> dict[str, Any]:
        config = core.load_config()
        root = _root_or_400(config)
        experiment = _valid_experiment(request.experiment)
        if not (root / "custom_data" / "dataset" / "2-name2text-0.txt").exists():
            raise HTTPException(status_code=400, detail="特征提取未完成，请先完成特征提取。")
        state = load_train_state()
        state.update(exp=experiment, s1_epochs=request.s1_epochs, s2_epochs=request.s2_epochs)
        save_train_state(state)
        commands, pre = cmd_train(str(root), experiment, request.s1_epochs, request.s2_epochs)
        job_id = start_job("train", "模型训练", commands, pre=pre, progress_meta={"s1_epochs": request.s1_epochs, "s2_epochs": request.s2_epochs, "gpu": os.environ.get("CUDA_VISIBLE_DEVICES", "0") or "0"})
        return {"job_id": job_id, **_job_message("train", "训练已启动：生成配置 -> s1 -> s2。")}

    @app.post("/api/training/stop")
    def training_stop(request: StopRequest) -> dict[str, Any]:
        job_id = request.job_id or last_job_id()
        if not job_id or not stop_job(job_id):
            raise HTTPException(status_code=400, detail="没有可停止的运行中任务。")
        return {"message": f"已请求停止任务 {job_id}", "log": _current_log(job_id)}

    @app.get("/api/training/log")
    def training_log(job_id: str | None = None, lines: int = Query(default=80, ge=1, le=500)) -> dict[str, str]:
        current = _current_log(job_id)
        if job_id and current["job_id"]:
            status = step_status(job_id.split("_")[0])
            path = status.get("log") or job_log_path(job_id)
            current["text"] = tail_log(path, lines) or current["text"]
        return current

    @app.post("/api/training/models/select")
    def select_models() -> dict[str, Any]:
        config = core.load_config()
        root = _root_or_400(config)
        models = find_models(str(root))
        if not models["gpt"] or not models["sovits"]:
            raise HTTPException(status_code=404, detail="还没找到训练产物，请先完成训练。")
        config.update(gpt_model=models["gpt"], sovits_model=models["sovits"])
        return {"message": "已填入并保存最新训练模型，可回合成页试音。", "config": core.save_config(config)}

    frontend_dist = Path(os.environ.get("XIANGHUA_FRONTEND_DIST", core.REPO_ROOT / "frontend" / "dist"))
    if frontend_dist.is_dir():
        assets = frontend_dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="frontend-assets")

        @app.get("/{frontend_path:path}", include_in_schema=False)
        def frontend(frontend_path: str) -> FileResponse:
            candidate = frontend_dist / frontend_path
            if frontend_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(frontend_dist / "index.html")

    return app


app = create_app()
