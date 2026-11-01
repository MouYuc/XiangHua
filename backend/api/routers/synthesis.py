"""Synthesis jobs (blocking request + SSE stream) and whole-episode export."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from backend.api import deps
from backend.schemas import (
    BatchExportRequest,
    SynthesisRequest,
)
from backend.services import (
    batch_production,
    core,
    reference_prompt,
    voice_assets,
)

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

@router.post("/batch/export")
def export_batch(request: BatchExportRequest) -> dict[str, Any]:
    snapshot = deps.SYNTHESIS_JOBS.snapshot(request.job_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="合成任务不存在。")
    if snapshot.get("status") != "done":
        raise HTTPException(status_code=409, detail="任务还没完成，暂时不能导出整集。")
    outputs = snapshot.get("outputs") or []
    if not any(item.get("file") for item in outputs):
        raise HTTPException(status_code=400, detail="该任务没有成功输出的台词，无法导出。")
    output_dir = deps.exported_output_dir(snapshot, outputs)
    try:
        result = batch_production.export_episode(outputs, output_dir)
    except batch_production.BatchExportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "message": f"已导出 {result['line_count']} 句（{result['failed']} 句缺失被跳过）。",
        **deps._public_export_result(result),
    }

# ------------------------------------------------------ 多角色对话编排 ----

@router.post("/synthesis")
def synthesis(request: SynthesisRequest, http_request: Request) -> dict[str, Any]:
    readiness = core.SYNTHESIS_BACKEND.warmup_status()
    if readiness["status"] in {"warming", "failed"}:
        raise HTTPException(status_code=409, detail=readiness["message"])
    config = deps._merge_config(request)
    # VoiceCard / StylePack overlay: explicit request fields always win over
    # the card's stored defaults, which in turn win over the saved config.
    if request.voice_card_id or request.style_id:
        try:
            if request.voice_card_id:
                card = deps.VOICE_CARDS.get(request.voice_card_id)
                config = voice_assets.apply_voice_card(
                    config,
                    card,
                    explicit_fields=request.model_fields_set,
                    style_id=request.style_id,
                )
            else:
                pack = voice_assets.resolve_style_pack(request.style_id)
                config = voice_assets.apply_style_pack(
                    config, pack, request.model_fields_set
                )
        except voice_assets.VoiceAssetError as exc:
            raise HTTPException(status_code=exc.http_status, detail=str(exc)) from exc
    try:
        prepared = deps.apply_voice_selection(
            config,
            request.text,
            request.voice_profile_id,
            request.accent_id,
        )
        pair = reference_prompt.reference_pair_check(
            prepared.config.get("ref_audio"),
            prepared.config.get("ref_text"),
        )
        if pair["status"] == "mismatched":
            raise HTTPException(
                status_code=400,
                detail=f"参考音频与参考文本不匹配：{pair['message']} 清单原文：{pair['expected_text']}",
            )
        deps._record_api_key_chars(http_request, request.text)
        return deps._public_synthesis_snapshot(
            deps.SYNTHESIS_JOBS.start(prepared.config, prepared.text)
        )
    except (RuntimeError, ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@router.get("/synthesis/status/{job_id}")
def synthesis_status(job_id: str) -> dict[str, Any]:
    snapshot = deps.SYNTHESIS_JOBS.snapshot(job_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="合成任务不存在。")
    return deps._public_synthesis_snapshot(snapshot)

@router.get("/synthesis/events/{job_id}")
async def synthesis_events(job_id: str) -> StreamingResponse:
    if deps.SYNTHESIS_JOBS.snapshot(job_id) is None:
        raise HTTPException(status_code=404, detail="合成任务不存在。")

    async def stream():
        async for event in deps.SYNTHESIS_JOBS.stream(job_id):
            yield f"data: {json.dumps(deps._public_synthesis_event(event), ensure_ascii=False)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
