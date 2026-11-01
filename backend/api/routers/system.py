"""Process-level routes: health, readiness, config, environment, jobs, doctor."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from backend.api import deps
from backend.schemas import (
    ConfigUpdate,
    DoctorRequest,
    EnvironmentSelectRequest,
)
from backend.services import (
    core,
    voice_assets,
)
from backend.services.doctor import run_doctor
from backend.services.gpu_arbiter import GPU_ARBITER
from backend.services.voice_selection import list_voice_options
from tools.env import derive_environment_config, scan_gpt_sovits_roots
from tools.train import (
    load_state as load_train_state,
)

router = APIRouter()


@router.get("/synthesis/readiness")
def synthesis_readiness() -> dict[str, Any]:
    status: dict[str, Any] = dict(core.SYNTHESIS_BACKEND.warmup_status())
    status["restart_required"] = core.SYNTHESIS_BACKEND.restart_required(core.load_config())
    return status

@router.post("/synthesis/readiness/retry")
def retry_synthesis_warmup() -> dict[str, str]:
    return core.SYNTHESIS_BACKEND.start_warmup(core.load_config())

@router.get("/config")
def get_config() -> dict[str, Any]:
    return core.load_config()

@router.put("/config")
def update_config(request: ConfigUpdate) -> dict[str, Any]:
    return core.save_config(deps._merge_config(request))

@router.get("/voice/options")
def voice_options() -> dict[str, Any]:
    options = list_voice_options(core.load_config())
    try:
        options["styles"] = [
            pack.public_dict() for pack in voice_assets.load_style_packs()
        ]
    except voice_assets.VoiceAssetError:
        options["styles"] = []
    return options

@router.get("/overview")
def overview() -> dict[str, Any]:
    config = core.load_config()
    return {
        "config": config,
        "environment": core.environment_check(config),
        "training": load_train_state(),
        "training_stats": core.training_stats(config),
        "history": core.load_history()[:5],
        "gpu": GPU_ARBITER.status(),
    }

@router.get("/jobs")
def list_jobs(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    """One place to see every background job the app is running."""
    return deps.JOB_REGISTRY.list_jobs(limit=limit)

@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = deps.JOB_REGISTRY.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    return {"job": job}

@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, Any]:
    if deps.JOB_REGISTRY.get(job_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    result = deps.JOB_REGISTRY.cancel(job_id)
    if not result.get("cancelled"):
        raise HTTPException(status_code=409, detail=result.get("reason") or "该任务无法取消。")
    return {"message": "已请求取消。", **result}

@router.get("/environment")
def environment() -> dict[str, Any]:
    config = core.load_config()
    return {"config": config, "rows": core.environment_check(config), "candidates": scan_gpt_sovits_roots(core.REPO_ROOT)}

@router.post("/environment/scan")
def scan_environment() -> dict[str, Any]:
    """Discover GPT-SoVITS trees on this machine. **Read-only.**

    This used to repoint ``gpt_sovits_root`` at the first candidate and persist
    it, which looked like a successful scan while the running backend — which had
    already imported a different tree — then refused every synthesis until a
    restart. Scanning is now a report: it names a recommendation and leaves the
    decision to ``/environment/select``.
    """
    config = core.load_config()
    candidates = scan_gpt_sovits_roots(core.REPO_ROOT)
    current = str(config.get("gpt_sovits_root") or "")
    known = {os.path.normcase(item["path"]) for item in candidates}
    recommended = candidates[0]["path"] if candidates and os.path.normcase(current) not in known else ""
    return {
        "config": config,
        "rows": core.environment_check(config),
        "candidates": candidates,
        "selected": current,
        "recommended": recommended,
        "restart_required": core.SYNTHESIS_BACKEND.restart_required(config),
    }

@router.post("/environment/select")
def select_environment(request: EnvironmentSelectRequest) -> dict[str, Any]:
    config = core.load_config()
    selected = Path(request.path).expanduser()
    if not (selected / "GPT_SoVITS").is_dir():
        raise HTTPException(status_code=400, detail="选择的 GPT-SoVITS 环境不存在或不完整。")
    # 只允许切换到本机扫描发现的候选环境（或当前已配置的环境），
    # 防止把引擎根目录指到任意目录并借此影响后续的导入与执行。
    known = {os.path.normcase(item["path"]) for item in scan_gpt_sovits_roots(core.REPO_ROOT)}
    known.add(os.path.normcase(str(config.get("gpt_sovits_root") or "")))
    if os.path.normcase(str(selected)) not in known:
        raise HTTPException(
            status_code=403,
            detail="只能选择环境扫描发现的 GPT-SoVITS 目录。",
        )
    config.update(derive_environment_config(str(selected), config))
    config = core.save_config(config)
    # Saving the new root is not the same as adopting it: the engine has already
    # imported a tree into ``sys.modules``, so a running process keeps executing
    # the old code. Say so instead of returning a silent 200.
    restart_required = core.SYNTHESIS_BACKEND.restart_required(config)
    message = (
        "已保存新环境。引擎已加载另一个环境，需要重启 API 服务后才会真正生效。"
        if restart_required
        else "已切换到新环境。"
    )
    return {
        "config": config,
        "rows": core.environment_check(config),
        "selected": str(selected),
        "restart_required": restart_required,
        "message": message,
    }

@router.post("/doctor")
def doctor(request: DoctorRequest) -> dict[str, Any]:
    return run_doctor(repair=request.repair)

@router.post("/engine/pronunciation-patch")
def import_pronunciation_patch(patch: UploadFile = File(...)) -> dict[str, str]:
    if os.environ.get("XIANGHUA_ALLOW_PRONUNCIATION_PATCH") != "1":
        raise HTTPException(
            status_code=403,
            detail=(
                "发音补丁导入未开启。此接口会改动引擎仓库文件，"
                "如确需使用，请设置环境变量 XIANGHUA_ALLOW_PRONUNCIATION_PATCH=1 后重启服务。"
            ),
        )
    root = core.load_config()["gpt_sovits_root"]
    if not (Path(root) / "GPT_SoVITS").is_dir():
        raise HTTPException(status_code=400, detail="GPT-SoVITS 根目录不可用。")
    try:
        message = core.apply_pronunciation_patch(root, deps._upload_filename(patch, ""), patch.file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="发音补丁导入失败。") from exc
    return {"message": message}


@router.get("/maintenance/retention")
def retention_report(
    max_age_days: float = Query(default=30.0, ge=1.0),
    max_output_dirs: int = Query(default=500, ge=1),
) -> dict[str, Any]:
    """What the retention policy *would* delete. Read-only."""
    from backend.services import retention

    return retention.prune(max_age_days=max_age_days, max_output_dirs=max_output_dirs, apply=False)


@router.post("/maintenance/prune")
def retention_prune(
    apply: bool = Query(default=False),
    max_age_days: float = Query(default=30.0, ge=1.0),
    max_output_dirs: int = Query(default=500, ge=1),
    job_snapshots: int = Query(default=200, ge=1),
) -> dict[str, Any]:
    """Delete old synthesised output. ``apply`` must be true to actually delete.

    Two-step on purpose: the report tells you exactly what would go, and nothing
    is removed until the caller asks for it.
    """
    from backend.services import retention

    if deps.SYNTHESIS_JOBS.active_count() > 0:
        raise HTTPException(status_code=409, detail="有合成任务正在运行，请等待完成后再清理。")
    return retention.prune(
        max_age_days=max_age_days,
        max_output_dirs=max_output_dirs,
        job_snapshots=job_snapshots,
        apply=apply,
    )
