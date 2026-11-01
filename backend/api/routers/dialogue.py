"""Multi-speaker dialogue orchestration."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from backend.api import deps
from backend.schemas import (
    DialogueExportRequest,
    DialogueSynthesisRequest,
)
from backend.services import (
    batch_production,
    core,
    dialogue,
)

router = APIRouter()


@router.post("/dialogue/synthesis")
def start_dialogue(request: DialogueSynthesisRequest) -> dict[str, Any]:
    if not request.script.strip():
        raise HTTPException(status_code=400, detail="对话脚本为空，请先输入内容。")
    known = {card["voice_id"] for card in deps.VOICE_CARDS.list_cards()}
    unknown = [vid for vid in request.roles.values() if vid not in known]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"角色绑定的声线卡不存在：{'、'.join(unknown)}。",
        )
    try:
        snapshot = dialogue.DIALOGUE_JOBS.start(
            request.script, request.roles, core.load_config()
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return deps._public_dialogue_snapshot(snapshot)

@router.get("/dialogue/status/{job_id}")
def dialogue_status(job_id: str) -> dict[str, Any]:
    snapshot = dialogue.DIALOGUE_JOBS.snapshot(job_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="对话任务不存在。")
    return deps._public_dialogue_snapshot(snapshot)

@router.post("/dialogue/export")
def export_dialogue(request: DialogueExportRequest) -> dict[str, Any]:
    snapshot = dialogue.DIALOGUE_JOBS.snapshot(request.job_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="对话任务不存在。")
    if snapshot.get("status") != "done":
        raise HTTPException(status_code=409, detail="对话还没合成完，暂时不能导出。")
    outputs = snapshot.get("outputs") or []
    if not any(item.get("file") for item in outputs):
        raise HTTPException(status_code=400, detail="该对话没有成功输出的台词，无法导出。")
    speakers = {
        index: str(item.get("speaker") or "")
        for index, item in enumerate(outputs)
        if item.get("speaker")
    }
    output_dir = deps.exported_output_dir(snapshot, outputs)
    try:
        result = batch_production.export_episode(outputs, output_dir, speakers=speakers)
    except batch_production.BatchExportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "message": f"已导出 {result['line_count']} 句（{result['failed']} 句失败被跳过），含角色分轨。",
        **deps._public_export_result(result),
    }

# ------------------------------------------------------ 溯源凭据校验 ----
