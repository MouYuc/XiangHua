"""Media ingest: upload, diarisation, speaker selection, dataset export."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from backend.api import deps
from backend.schemas import (
    MediaAnalyzeRequest,
    MediaExportRequest,
    MediaSelectSpeakerRequest,
)
from backend.services.training import (
    ST_DONE,
    ST_PENDING,
    mark_step,
)

router = APIRouter()


@router.post("/media/sessions")
def create_media_session(
    recording: list[UploadFile] = File(...),
    name: str = Form(""),
    language: str = Form("zh"),
    rights_declared: bool = Form(False),
    rights_note: str = Form(""),
    transcribe: bool = Form(False),
    speaker_count: int | None = Form(default=None),
) -> dict[str, Any]:
    service = deps._media_service()
    files = [
        (deps._upload_filename(item, "media"), item.file)
        for item in recording
        if item is not None and item.filename
    ]
    if not files:
        raise HTTPException(status_code=400, detail="没有收到任何媒体文件。")
    first_name = files[0][0]
    session_name = name or Path(first_name).stem
    if len(files) > 1:
        session_name = f"{Path(first_name).stem} 等 {len(files)} 个文件"
    session = service.create_session(
        session_name,
        language=language,
        rights_declared=rights_declared,
        rights_note=rights_note,
    )
    # Transcoding runs on a worker thread and chains into the analysis, so the
    # response returns immediately with ``job.status == "uploading"``. Starting
    # the analysis from here would race the transcode (the source does not exist
    # yet) and surface as "还没有上传媒体"。
    session = service.ingest_files(
        session["session_id"],
        files,
        autostart_analysis=True,
        speaker_count=speaker_count,
        transcribe=transcribe,
    )
    message = (
        f"已上传 {len(files)} 个媒体文件，正在转码并分析。"
        if len(files) > 1
        else "媒体已上传，正在转码并分析。"
    )
    return {
        "message": message,
        "session": deps._public_media_session(service, service.load(session["session_id"])),
    }

@router.get("/media/sessions")
def list_media_sessions() -> dict[str, Any]:
    service = deps._media_service()
    return {"rows": service.list_sessions()}

@router.get("/media/sessions/{session_id}")
def get_media_session(session_id: str) -> dict[str, Any]:
    service = deps._media_service()
    return {"session": deps._public_media_session(service, service.load(session_id))}

@router.post("/media/sessions/{session_id}/analyze")
def analyze_media_session(session_id: str, request: MediaAnalyzeRequest) -> dict[str, Any]:
    service = deps._media_service()
    service.start_analysis(
        session_id,
        speaker_count=request.speaker_count,
        transcribe=request.transcribe,
        model=request.model,
    )
    return {
        "message": "分析已启动。",
        "session": deps._public_media_session(service, service.load(session_id)),
    }

@router.get("/media/sessions/{session_id}/job")
def media_session_job(session_id: str) -> dict[str, Any]:
    service = deps._media_service()
    session = service.load(session_id)
    return {"job": session.get("job") or {}}

@router.post("/media/sessions/{session_id}/select-speaker")
def select_media_speaker(session_id: str, request: MediaSelectSpeakerRequest) -> dict[str, Any]:
    service = deps._media_service()
    session = service.select_speaker(session_id, request.speaker_id)
    return {
        "message": "已选择目标说话人。",
        "session": deps._public_media_session(service, session),
    }

@router.post("/media/sessions/{session_id}/export")
def export_media_dataset(session_id: str, request: MediaExportRequest) -> dict[str, Any]:
    service = deps._media_service()
    dataset = service.export_dataset(
        session_id,
        speaker_id=request.speaker_id,
        include_failed=request.include_failed,
        require_text=request.require_text,
        run_id=request.run_id,
    )
    public = deps._public_media_session(service, service.load(session_id))
    return {
        "message": "训练集已生成。",
        "dataset": public.get("dataset") or dataset,
        "session": public,
    }

@router.post("/media/sessions/{session_id}/activate")
def activate_media_dataset(session_id: str, request: MediaExportRequest) -> dict[str, Any]:
    service = deps._media_service()
    result = service.activate_dataset(
        session_id,
        speaker_id=request.speaker_id,
        include_failed=request.include_failed,
        require_text=request.require_text,
        dataset_id=request.dataset_id,
        run_id=request.run_id,
    )
    mark_step("slice", ST_DONE, note=f"媒体采集导入 {result['copied']} 段")
    if result.get("text_ready"):
        mark_step("asr", ST_DONE, note="媒体采集自动转写")
    mark_step("correct", ST_DONE, note=f"媒体采集导入 {result['copied']} 句")
    # 换了新素材，旧数据集的「特征提取/训练已完成」状态必须归零：训练状态是全局
    # 的，若保留 done，界面会允许直接训练，实际跑在上一份数据集的特征上。
    mark_step("feat", ST_PENDING, note="新素材已导入，需重新提取特征")
    mark_step("train", ST_PENDING, note="新素材已导入，需重新提取特征")
    return {
        "message": f"已写入训练工作区：{result['copied']} 句。回到训练页即可做特征提取。",
        **result,
    }

@router.get("/media/sessions/{session_id}/dataset/{dataset_id}/{filename}")
def serve_media_dataset_file(session_id: str, dataset_id: str, filename: str) -> FileResponse:
    service = deps._media_service()
    session = service.load(session_id)
    dataset = session.get("dataset") or {}
    if dataset.get("dataset_id") != dataset_id:
        raise HTTPException(status_code=404, detail="数据集不存在。")
    base = (service.session_dir(session_id) / str(dataset.get("directory") or "")).resolve()
    target = (base / filename).resolve()
    if base not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="数据集文件不存在。")
    return FileResponse(target, filename=target.name)

# Mount after every route is registered, before the SPA catch-all. ``/api``
# stays the internal WebUI surface; ``/v1`` is what SDKs and customers call.
