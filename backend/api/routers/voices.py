"""Voice assets: voice cards, reference audio preparation, user lexicon."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response

from backend.api import deps
from backend.schemas import (
    LexiconUpsertRequest,
    ReferenceAudioRequest,
    VoiceCardCreate,
    VoiceCardUpdate,
    VoiceCardVersionCreate,
)
from backend.services import (
    core,
    reference_audio,
    reference_prompt,
    user_lexicon,
    voice_assets,
    voice_ingest,
)
from backend.services.path_guard import PathConfinementError, confined_directory, confined_file

router = APIRouter()


@router.post("/reference-audio")
def upload_reference_audio(
    audio: UploadFile = File(...),
    purpose: str = Form("synthesis"),
    auto_prepare: bool = Form(True),
    language: str = Form("zh"),
) -> dict[str, Any]:
    """Accept one natural recording and prepare a synthesis prompt automatically.

    The user is not asked to read a script or type the transcript. For the
    synthesis path we select a 2-10 s speech window, gate it, and transcribe it.
    The original upload is still saved, so a failed gate does not destroy the
    asset and a later timbre adapter can use the full recording.
    """
    try:
        path = core.save_reference_audio(deps._upload_filename(audio, ""), audio.file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="参考音频保存失败。") from exc

    if auto_prepare and purpose == "synthesis":
        try:
            prepared = voice_ingest.prepare_reference_from_audio(
                path,
                core.REFERENCE_AUDIO_DIR / "prepared",
                language=language,
            )
        except voice_ingest.VoiceIngestError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"自动准备音色样本失败：{exc}") from exc
        return {
            "path": prepared["ref_audio"],
            "source_path": str(path),
            "ref_text": prepared["ref_text"],
            "auto_prepared": True,
            "selected": prepared["selected"],
            "transcription": prepared["transcription"],
            "warnings": prepared["warnings"],
            "report": prepared["quality"],
        }

    try:
        report = reference_audio.judge(path, purpose)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # A gate that cannot read the file must not discard a saved upload.
        report = {"status": "unknown", "error": f"{type(exc).__name__}: {exc}"}
    return {
        "path": str(path),
        "source_path": str(path),
        "ref_text": "",
        "auto_prepared": False,
        "warnings": [],
        "report": report,
    }

@router.post("/reference-audio/prepare")
def start_reference_audio_prepare(
    audio: UploadFile = File(...),
    purpose: str = Form("synthesis"),
    auto_prepare: bool = Form(True),
    language: str = Form("zh"),
) -> dict[str, str]:
    """Save the upload, then prepare the prompt in a background job.

    The synchronous route above is kept for compatibility, but it holds the
    HTTP request for the whole slicing/gate/ASR pass. The WebUI posts here and
    polls ``GET /api/reference-audio/prepare/{job_id}`` instead, so a long
    recording cannot hang the browser request.
    """
    if not auto_prepare or purpose != "synthesis":
        raise HTTPException(
            status_code=400,
            detail="该接口只用于自动准备合成参考音；仅上传检查请使用 POST /api/reference-audio。",
        )
    try:
        path = core.save_reference_audio(deps._upload_filename(audio, ""), audio.file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="参考音频保存失败。") from exc
    snapshot = deps.VOICE_PREPARE_JOBS.start(
        path,
        core.REFERENCE_AUDIO_DIR / "prepared",
        language=language,
    )
    return {"job_id": str(snapshot["job_id"]), "status": str(snapshot["status"])}

@router.get("/reference-audio/prepare/{job_id}")
def reference_audio_prepare_status(job_id: str) -> dict[str, Any]:
    """Return the latest snapshot of one background voice preparation job."""
    snapshot = deps.VOICE_PREPARE_JOBS.snapshot(job_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="音色准备任务不存在。")
    return snapshot

@router.get("/voice-cards")
def list_voice_cards() -> dict[str, Any]:
    return {
        "cards": [voice_assets.card_public_dict(card) for card in deps.VOICE_CARDS.list_cards()]
    }

@router.post("/voice-cards")
def create_voice_card(request: VoiceCardCreate) -> dict[str, Any]:
    """Snapshot the current reference/models into a new versioned VoiceCard."""
    try:
        card = deps.VOICE_CARDS.create(
            core.load_config(),
            display_name=request.display_name,
            language=request.language,
            license_id=request.license_id,
            consent_id=request.consent_id,
            notes=request.notes,
        )
    except voice_assets.VoiceAssetError as exc:
        raise HTTPException(status_code=exc.http_status, detail=str(exc)) from exc
    return {
        "message": (
            f"已保存声线卡「{card['display_name']}」"
            f"（{card['voice_id']} v{card['active_version']}）。"
        ),
        "card": voice_assets.card_public_dict(card),
    }

@router.get("/voice-cards/{voice_id}")
def get_voice_card(voice_id: str) -> dict[str, Any]:
    try:
        card = deps.VOICE_CARDS.get(voice_id)
    except voice_assets.VoiceAssetError as exc:
        raise HTTPException(status_code=exc.http_status, detail=str(exc)) from exc
    return {"card": voice_assets.card_public_dict(card)}

@router.patch("/voice-cards/{voice_id}")
def update_voice_card(voice_id: str, request: VoiceCardUpdate) -> dict[str, Any]:
    try:
        card = deps.VOICE_CARDS.update(
            voice_id,
            display_name=request.display_name,
            status=request.status,
            license_id=request.license_id,
            consent_id=request.consent_id,
            active_version=request.active_version,
        )
    except voice_assets.VoiceAssetError as exc:
        raise HTTPException(status_code=exc.http_status, detail=str(exc)) from exc
    return {
        "message": f"已更新声线卡「{card['display_name']}」。",
        "card": voice_assets.card_public_dict(card),
    }

@router.post("/voice-cards/{voice_id}/versions")
def add_voice_card_version(
    voice_id: str, request: VoiceCardVersionCreate
) -> dict[str, Any]:
    """Append a new immutable version captured from the current config."""
    try:
        card = deps.VOICE_CARDS.add_version(voice_id, core.load_config(), notes=request.notes)
    except voice_assets.VoiceAssetError as exc:
        raise HTTPException(status_code=exc.http_status, detail=str(exc)) from exc
    return {
        "message": f"已新增版本 v{card['active_version']} 并设为当前版本。",
        "card": voice_assets.card_public_dict(card),
    }

@router.post("/voice-cards/{voice_id}/activate")
def activate_voice_card(voice_id: str) -> dict[str, Any]:
    """Write the card's active version into the saved synthesis config."""
    try:
        card = deps.VOICE_CARDS.get(voice_id)
        if str(card.get("status") or "active") != "active":
            raise voice_assets.VoiceAssetError(
                f"声线卡已停用：{card.get('display_name')}，不能激活。"
            )
        before = core.load_config()
        config = voice_assets.apply_voice_card(before, card)
    except voice_assets.VoiceAssetError as exc:
        raise HTTPException(status_code=exc.http_status, detail=str(exc)) from exc
    applied = [
        key
        for key in ("gpt_model", "sovits_model", "ref_audio", "ref_text", "prompt_lang",
                    "accent_level", "speed", "pause_second")
        if config.get(key) != before.get(key)
    ]
    core.save_config(config)
    return {
        "message": (
            f"已激活「{card['display_name']}」v{card['active_version']}："
            f"{'、'.join(applied) if applied else '配置无变化'}。"
        ),
        "voice_card_id": card["voice_id"],
        "active_version": card["active_version"],
        "applied": applied,
    }


# ------------------------------------------------------ 用户发音词典 ----
# 多音字/专名读音修正：最高优先级的发音替换规则，见 services/user_lexicon.py。

@router.get("/lexicon")
def list_lexicon() -> dict[str, Any]:
    return {"entries": user_lexicon.list_entries()}

@router.post("/lexicon")
def upsert_lexicon(request: LexiconUpsertRequest) -> dict[str, Any]:
    try:
        row = user_lexicon.upsert_entry(
            request.term, request.replacement, note=request.note, enabled=request.enabled
        )
    except user_lexicon.LexiconError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "message": f"已保存词条「{row['term']} → {row['replacement']}」，下次生成即生效。",
        "entry": row,
    }

@router.delete("/lexicon", status_code=204)
def delete_lexicon(term: str = Query(...)) -> Response:
    if not user_lexicon.delete_entry(term):
        raise HTTPException(status_code=404, detail="词条不存在。")
    return Response(status_code=204)

# ------------------------------------------------------ 整集交付导出 ----
# 把一次合成/对话任务的逐句输出导出为 episode.srt + 整集音轨 + 角色分轨。

@router.get("/reference-audio/prompt")
def reference_audio_prompt(
    path: str = Query(..., description="参考音频路径"),
    ref_text: str = Query("", description="当前填写的参考文本"),
) -> dict[str, Any]:
    """Look up a known slice transcript and compare it with the user's text."""
    return {
        "prompt": reference_prompt.lookup_reference_prompt(path),
        "pair": reference_prompt.reference_pair_check(path, ref_text),
    }

@router.post("/reference-audio/inspect")
def inspect_reference_audio(request: ReferenceAudioRequest) -> dict[str, Any]:
    """Grade a clip that is already on disk, or a whole corpus directory."""
    try:
        if request.directory:
            paths = reference_audio.collect_audio(confined_directory(request.directory))
            if not paths:
                raise HTTPException(status_code=404, detail="该目录里没有音频文件。")
            return {"kind": "corpus", "report": reference_audio.analyze_corpus(paths, request.purpose)}
        if not request.path:
            raise HTTPException(status_code=400, detail="请提供 path 或 directory。")
        candidate = confined_file(request.path)
        if not candidate.is_file():
            raise HTTPException(status_code=404, detail="找不到要检查的音频。")
        return {
            "kind": "file",
            "report": reference_audio.judge(candidate, request.purpose),
            "pair": (
                reference_prompt.reference_pair_check(candidate, request.ref_text)
                if request.ref_text is not None
                else None
            ),
        }
    except HTTPException:
        raise
    except PathConfinementError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"检查失败：{exc}") from exc
