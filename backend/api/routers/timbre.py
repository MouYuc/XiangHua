"""Timbre conversion and voice-profile endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile

from backend.api import deps
from backend.schemas import (
    TimbreConvertRequest,
    TimbreProfileRequest,
)
from backend.services import (
    core,
    timbre,
)
from backend.services.path_guard import PathConfinementError, confined_file

router = APIRouter()


@router.get("/timbre/profile")
def timbre_profile(profile_id: str = Query(default="current")) -> dict[str, Any]:
    """Report a voice profile's reference corpus, cache state and VRAM."""
    return deps.TIMBRE_BACKEND.profile_status(profile_id, core.load_config())

@router.post("/timbre/profile/build")
def timbre_profile_build(request: TimbreProfileRequest) -> dict[str, Any]:
    """Encode a profile's reference audio into a cached kNN matching pool."""
    config = core.load_config()
    try:
        return deps._public_timbre_snapshot(
            deps.TIMBRE_BACKEND.start_profile_build(request.profile_id, config, device=request.device)
        )
    except (RuntimeError, ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@router.get("/timbre/sources")
def timbre_sources(limit: int = Query(default=30, ge=1, le=200)) -> dict[str, Any]:
    """List audio the user can convert: synthesis history plus past uploads."""
    return {"rows": timbre.list_sources(limit)}

@router.post("/timbre/convert")
async def timbre_convert_upload(
    audio: UploadFile = File(...),
    topk: int = Form(4),
    profile_id: str = Form("current"),
    device: str | None = Form(None),
) -> dict[str, Any]:
    """Convert an uploaded recording (speech, or a solo singing take) into a profile's voice.

    A *finished song* does not belong here: its vocals would have to be separated from
    the accompaniment first, and the singing-voice replacement route was removed.
    """
    if topk < 1:
        raise HTTPException(status_code=400, detail="topk 必须 >= 1。")
    try:
        source = timbre.save_source(deps._upload_filename(audio, "source.wav"), audio.file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="源音频保存失败。") from exc
    config = core.load_config()
    return deps._public_timbre_snapshot(
        deps.TIMBRE_BACKEND.start_conversion(source, profile_id, config, topk=topk, device=device)
    )

@router.post("/timbre/convert-file")
def timbre_convert_file(request: TimbreConvertRequest) -> dict[str, Any]:
    """Convert a file already on disk, e.g. a synthesis result just produced."""
    source: Path | None = None
    if request.download_id:
        source = core.resolve_download(request.download_id)
    if source is None and request.source_path:
        try:
            candidate = confined_file(request.source_path)
        except PathConfinementError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        source = candidate if candidate.is_file() else None
    if source is None or not source.is_file():
        raise HTTPException(status_code=404, detail="找不到要转换的源音频。")
    config = core.load_config()
    try:
        return deps._public_timbre_snapshot(
            deps.TIMBRE_BACKEND.start_conversion(
                source, request.profile_id, config, topk=request.topk, device=request.device
            )
        )
    except (RuntimeError, ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@router.get("/timbre/status/{job_id}")
def timbre_status(job_id: str) -> dict[str, Any]:
    snapshot = deps.TIMBRE_BACKEND.snapshot(job_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="音色任务不存在。")
    return deps._public_timbre_snapshot(snapshot)

@router.get("/timbre/jobs")
def timbre_jobs(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    return {"rows": [deps._public_timbre_snapshot(row) for row in deps.TIMBRE_BACKEND.list_jobs(limit)]}

@router.post("/timbre/unload")
def timbre_unload() -> dict[str, Any]:
    """Release the encoder so synthesis can reclaim the GPU."""
    return deps.TIMBRE_BACKEND.unload()
