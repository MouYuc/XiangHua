"""FastAPI application for the Vue XiangHua WebUI."""

from __future__ import annotations

import copy
import os
import re
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
import json

from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.schemas import (
    ApiKeyCreateRequest,
    BatchExportRequest,
    ConfigUpdate,
    DialogueExportRequest,
    DialogueSynthesisRequest,
    DoctorRequest,
    EnvironmentSelectRequest,
    FeatureRequest,
    LexiconUpsertRequest,
    MediaAnalyzeRequest,
    MediaExportRequest,
    MediaSelectSpeakerRequest,
    ModelRunRequest,
    ProvenanceVerifyRequest,
    ReferenceAudioRequest,
    StopRequest,
    SynthesisRequest,
    TimbreConvertRequest,
    TimbreProfileRequest,
    TrainRequest,
    TranscriptUpdate,
    VoiceCardCreate,
    VoiceCardUpdate,
    VoiceCardVersionCreate,
)
from backend.services import (
    batch_production,
    core,
    dialogue,
    media_ingest,
    provenance,
    reference_audio,
    reference_prompt,
    timbre,
    user_lexicon,
    voice_assets,
    voice_ingest,
)
from backend.services.doctor import run_doctor
from backend.services.gpu_arbiter import GPU_ARBITER, GpuBusyTimeout
from backend.services.job_registry import JOB_REGISTRY
from backend.services.job_store import JobStore
from backend.services.path_guard import PathConfinementError, confined_directory, confined_file
from backend.services.request_guard import RequestGuardMiddleware, api_key_registry
from backend.services.synthesis_jobs import SYNTHESIS_JOBS
from backend.services.timbre import TIMBRE_BACKEND
from backend.services.voice_assets import VoiceCardStore
from backend.services.voice_prepare_jobs import VOICE_PREPARE_JOBS
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


# Versioned VoiceCard registry; replaced wholesale in tests via patch.object.
VOICE_CARDS = VoiceCardStore()


def _merge_config(update: ConfigUpdate) -> dict[str, Any]:
    config = core.load_config()
    payload = update.model_dump(
        exclude_none=True,
        exclude={"text", "output_dir", "zip_output", "voice_card_id", "style_id"},
    )
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


def _record_api_key_chars(http_request: Request, text: str) -> None:
    """Meter synthesised characters against the calling tenant's API key."""
    registry = api_key_registry()
    if registry is None:
        return
    try:
        token = http_request.headers.get("x-api-key") or ""
        if not token:
            auth = http_request.headers.get("authorization") or ""
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
        record = registry.authenticate(token) if token else None
        if record:
            registry.record_chars(str(record.get("key_id") or ""), len(text or ""))
    except Exception:
        pass  # 计量失败绝不影响合成


def _registered_url(path_value: str | Path) -> str:
    """Download URL for an export artifact, or "" when it cannot be registered."""
    try:
        candidate = Path(str(path_value or ""))
        return _as_url(core.register_download(candidate)) if candidate.is_file() else ""
    except Exception:
        return ""


def exported_output_dir(snapshot: dict[str, Any], outputs: list[dict[str, Any]]) -> Path:
    """Directory an episode export belongs in: the job's folder, else the first file's.

    ``Path("")`` is ``.`` and therefore truthy, so the old
    ``Path(...) or fallback`` never actually fell back — dialogue jobs (which
    used to store an empty ``output_dir``) wrote ``episode.srt`` into whatever
    directory the server process was started from.
    """
    raw = str(snapshot.get("output_dir") or "").strip()
    if raw:
        return Path(raw)
    first = next((item.get("file") for item in outputs if item.get("file")), "")
    return Path(str(first)).parent if first else Path.cwd()


def _public_export_result(result: dict[str, Any]) -> dict[str, Any]:
    """Attach download URLs to an episode export without exposing raw paths."""
    data = dict(result)
    data["srt_url"] = _registered_url(data.get("srt") or "")
    data["full_url"] = _registered_url(data.get("full") or "")
    stems = data.get("stems") or {}
    data["stems"] = {speaker: path for speaker, path in stems.items()}
    data["stem_urls"] = {speaker: _registered_url(path) for speaker, path in stems.items()}
    return data


def _public_dialogue_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(snapshot or {})
    outputs: list[dict[str, Any]] = []
    for item in data.get("outputs") or []:
        row = dict(item)
        row.pop("file", None)
        row["url"] = _registered_url(str(item.get("file") or ""))
        outputs.append(row)
    data["outputs"] = outputs
    return data


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
    """Attach playable URLs to a finished conversion result.

    The result files live under ``output/vc/webui``; registering them as downloads is
    what lets the browser play them without exposing the filesystem path. Mix
    replacement produces several files, and all of them are useful: the finished song, the
    isolated converted vocal (so the user can hear what actually replaced the voice), and
    the extracted original vocal (so the user can judge the separator).
    """
    data = dict(snapshot or {})
    result = data.get("result")
    if isinstance(result, dict) and result.get("file"):
        row = dict(result)
        path = Path(str(row.pop("file")))
        row["url"] = _as_url(core.register_download(path)) if path.is_file() else ""
        for key, url_key in (
            ("converted_vocal", "converted_vocal_url"),
            ("vocal_stem", "vocal_stem_url"),
        ):
            candidate = Path(str(row.pop(key, "") or ""))
            row[url_key] = _as_url(core.register_download(candidate)) if candidate.is_file() else ""
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


def _media_service(config: dict[str, Any] | None = None) -> media_ingest.MediaIngestService:
    cfg = config or core.load_config()
    root = _root_or_400(cfg)
    checkpoint = root / "GPT_SoVITS" / "pretrained_models" / "sv" / "pretrained_eres2netv2w24s4ep4.ckpt"
    return media_ingest.MediaIngestService(root, checkpoint=checkpoint if checkpoint.is_file() else None)


def _public_media_session(service: media_ingest.MediaIngestService, session: dict[str, Any]) -> dict[str, Any]:
    """Attach download URLs without exposing the session directory to the browser."""
    data = copy.deepcopy(session)
    session_dir = service.session_dir(session["session_id"])
    segments = data.get("segments") or []
    for segment in segments:
        path = session_dir / str(segment.get("path") or "")
        segment["audio_url"] = _as_url(core.register_download(path)) if path.is_file() else ""
    for speaker in data.get("speakers") or []:
        sample = next(
            (item for item in segments if item.get("speaker_id") == speaker.get("speaker_id")),
            None,
        )
        speaker["sample_url"] = (sample or {}).get("audio_url", "")
    dataset = data.get("dataset")
    if isinstance(dataset, dict) and dataset.get("dataset_id"):
        dataset_id = str(dataset["dataset_id"])
        for key, filename in (
            ("list", "dataset.list"),
            ("manifest", "manifest.json"),
            ("checksums", "checksums.sha256"),
        ):
            path = session_dir / str(dataset.get(key) or "")
            dataset[f"{key}_url"] = (
                f"/api/media/sessions/{session['session_id']}/dataset/{dataset_id}/{filename}"
                if path.is_file()
                else ""
            )
    return data


_ERROR_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    422: "INVALID_ARGUMENT",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
}


def _error_code(status: int) -> str:
    """Stable machine-readable code for an HTTP status."""
    return _ERROR_CODES.get(int(status), f"HTTP_{int(status)}")


def _upload_filename(file: UploadFile, fallback: str = "upload") -> str:
    """Recover a UTF-8 multipart filename that Starlette decoded as latin-1.

    Starlette falls back to latin-1 whenever the part carries no explicit charset,
    so a Chinese name sent by curl/browser shows up as ``ºúÌÒ...`` in session
    metadata and on disk. Round-tripping the string through latin-1 recovers the
    original bytes; a name that is already real UTF-8 contains characters outside
    latin-1 and is returned unchanged instead of being mangled.
    """
    raw = file.filename or fallback
    try:
        return raw.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return raw


def create_app() -> FastAPI:
    app = FastAPI(title="XiangHua API", version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # 注册在 CORS 之后 = 位于最外层：Host/Origin 校验、/v1 API Key 与合成
    # 队列上限在任何路由处理之前拒绝请求。仅作用于 /api 与 /v1 前缀。
    app.add_middleware(RequestGuardMiddleware)

    # Every product route lives on this router and is mounted twice: ``/api`` for
    # the bundled WebUI, ``/v1`` as the public contract. One handler, two prefixes,
    # so an internal UI change can never silently shift the API surface.
    router = APIRouter()

    @app.on_event("startup")
    def start_synthesis_warmup() -> None:
        # 任务快照落盘：重启后历史任务仍可见（运行中的标记为中断）。
        try:
            SYNTHESIS_JOBS.enable_persistence(JobStore(core.CONFIG_DIR))
        except Exception:
            pass
        core.SYNTHESIS_BACKEND.start_warmup(core.load_config())

    @router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/synthesis/readiness")
    def synthesis_readiness() -> dict[str, str]:
        return core.SYNTHESIS_BACKEND.warmup_status()

    @router.post("/synthesis/readiness/retry")
    def retry_synthesis_warmup() -> dict[str, str]:
        return core.SYNTHESIS_BACKEND.start_warmup(core.load_config())

    @router.get("/config")
    def get_config() -> dict[str, Any]:
        return core.load_config()

    @router.put("/config")
    def update_config(request: ConfigUpdate) -> dict[str, Any]:
        return core.save_config(_merge_config(request))

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
        return JOB_REGISTRY.list_jobs(limit=limit)

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        job = JOB_REGISTRY.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务不存在。")
        return {"job": job}

    @router.post("/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, Any]:
        if JOB_REGISTRY.get(job_id) is None:
            raise HTTPException(status_code=404, detail="任务不存在。")
        result = JOB_REGISTRY.cancel(job_id)
        if not result.get("cancelled"):
            raise HTTPException(status_code=409, detail=result.get("reason") or "该任务无法取消。")
        return {"message": "已请求取消。", **result}

    @router.get("/environment")
    def environment() -> dict[str, Any]:
        config = core.load_config()
        return {"config": config, "rows": core.environment_check(config), "candidates": scan_gpt_sovits_roots(core.REPO_ROOT)}

    @router.post("/environment/scan")
    def scan_environment() -> dict[str, Any]:
        config = core.load_config()
        candidates = scan_gpt_sovits_roots(core.REPO_ROOT)
        selected = str(config.get("gpt_sovits_root") or "")
        if not any(os.path.normcase(item["path"]) == os.path.normcase(selected) for item in candidates) and candidates:
            selected = candidates[0]["path"]
            config.update(derive_environment_config(selected, config))
            config = core.save_config(config)
        return {"config": config, "rows": core.environment_check(config), "candidates": candidates, "selected": selected}

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
        return {"config": config, "rows": core.environment_check(config), "selected": str(selected)}

    @router.post("/doctor")
    def doctor(request: DoctorRequest) -> dict[str, Any]:
        return run_doctor(repair=request.repair)

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
            path = core.save_reference_audio(_upload_filename(audio, ""), audio.file)
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
            path = core.save_reference_audio(_upload_filename(audio, ""), audio.file)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail="参考音频保存失败。") from exc
        snapshot = VOICE_PREPARE_JOBS.start(
            path,
            core.REFERENCE_AUDIO_DIR / "prepared",
            language=language,
        )
        return {"job_id": str(snapshot["job_id"]), "status": str(snapshot["status"])}

    @router.get("/reference-audio/prepare/{job_id}")
    def reference_audio_prepare_status(job_id: str) -> dict[str, Any]:
        """Return the latest snapshot of one background voice preparation job."""
        snapshot = VOICE_PREPARE_JOBS.snapshot(job_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="音色准备任务不存在。")
        return snapshot

    @router.get("/voice-cards")
    def list_voice_cards() -> dict[str, Any]:
        return {
            "cards": [voice_assets.card_public_dict(card) for card in VOICE_CARDS.list_cards()]
        }

    @router.post("/voice-cards")
    def create_voice_card(request: VoiceCardCreate) -> dict[str, Any]:
        """Snapshot the current reference/models into a new versioned VoiceCard."""
        try:
            card = VOICE_CARDS.create(
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
            card = VOICE_CARDS.get(voice_id)
        except voice_assets.VoiceAssetError as exc:
            raise HTTPException(status_code=exc.http_status, detail=str(exc)) from exc
        return {"card": voice_assets.card_public_dict(card)}

    @router.patch("/voice-cards/{voice_id}")
    def update_voice_card(voice_id: str, request: VoiceCardUpdate) -> dict[str, Any]:
        try:
            card = VOICE_CARDS.update(
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
            card = VOICE_CARDS.add_version(voice_id, core.load_config(), notes=request.notes)
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
            card = VOICE_CARDS.get(voice_id)
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

    @router.post("/batch/export")
    def export_batch(request: BatchExportRequest) -> dict[str, Any]:
        snapshot = SYNTHESIS_JOBS.snapshot(request.job_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="合成任务不存在。")
        if snapshot.get("status") != "done":
            raise HTTPException(status_code=409, detail="任务还没完成，暂时不能导出整集。")
        outputs = snapshot.get("outputs") or []
        if not any(item.get("file") for item in outputs):
            raise HTTPException(status_code=400, detail="该任务没有成功输出的台词，无法导出。")
        output_dir = exported_output_dir(snapshot, outputs)
        try:
            result = batch_production.export_episode(outputs, output_dir)
        except batch_production.BatchExportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "message": f"已导出 {result['line_count']} 句（{result['failed']} 句缺失被跳过）。",
            **_public_export_result(result),
        }

    # ------------------------------------------------------ 多角色对话编排 ----

    @router.post("/dialogue/synthesis")
    def start_dialogue(request: DialogueSynthesisRequest) -> dict[str, Any]:
        if not request.script.strip():
            raise HTTPException(status_code=400, detail="对话脚本为空，请先输入内容。")
        known = {card["voice_id"] for card in VOICE_CARDS.list_cards()}
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
        return _public_dialogue_snapshot(snapshot)

    @router.get("/dialogue/status/{job_id}")
    def dialogue_status(job_id: str) -> dict[str, Any]:
        snapshot = dialogue.DIALOGUE_JOBS.snapshot(job_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="对话任务不存在。")
        return _public_dialogue_snapshot(snapshot)

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
        output_dir = exported_output_dir(snapshot, outputs)
        try:
            result = batch_production.export_episode(outputs, output_dir, speakers=speakers)
        except batch_production.BatchExportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "message": f"已导出 {result['line_count']} 句（{result['failed']} 句失败被跳过），含角色分轨。",
            **_public_export_result(result),
        }

    # ------------------------------------------------------ 溯源凭据校验 ----

    @router.post("/provenance/verify")
    def verify_provenance_route(request: ProvenanceVerifyRequest) -> dict[str, Any]:
        source: Path | None = None
        if request.download_id:
            source = core.resolve_download(request.download_id)
        if source is None and request.path:
            try:
                source = confined_file(request.path)
            except PathConfinementError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
        if source is None or not source.is_file():
            raise HTTPException(status_code=404, detail="找不到要校验的音频文件。")
        return provenance.verify_provenance(source)

    # ------------------------------------------------------ 租户 API Key ----
    # 管理 XIANGHUA_API_KEYS_FILE 指向的多租户密钥注册表；token 只在创建时返回一次。

    @router.get("/api-keys")
    def list_api_keys() -> dict[str, Any]:
        registry = api_key_registry()
        if registry is None:
            raise HTTPException(
                status_code=409,
                detail="未配置多租户密钥：请设置环境变量 XIANGHUA_API_KEYS_FILE 后重启服务。",
            )
        return {"keys": registry.list_keys()}

    @router.post("/api-keys")
    def create_api_key(request: ApiKeyCreateRequest) -> dict[str, Any]:
        registry = api_key_registry()
        if registry is None:
            raise HTTPException(
                status_code=409,
                detail="未配置多租户密钥：请设置环境变量 XIANGHUA_API_KEYS_FILE 后重启服务。",
            )
        try:
            record = registry.create_key(
                request.tenant,
                label=request.label,
                daily_request_quota=request.daily_request_quota,
                rpm_limit=request.rpm_limit,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "message": (
                f"已为租户「{record['tenant']}」创建 API Key。"
                "令牌只显示这一次，请立即保存。"
            ),
            "key": record,
        }

    @router.delete("/api-keys/{key_id}", status_code=204)
    def revoke_api_key(key_id: str) -> Response:
        registry = api_key_registry()
        if registry is None:
            raise HTTPException(
                status_code=409,
                detail="未配置多租户密钥：请设置环境变量 XIANGHUA_API_KEYS_FILE 后重启服务。",
            )
        if not registry.revoke_key(key_id):
            raise HTTPException(status_code=404, detail="API Key 不存在。")
        return Response(status_code=204)

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
            message = core.apply_pronunciation_patch(root, _upload_filename(patch, ""), patch.file)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail="发音补丁导入失败。") from exc
        return {"message": message}

    @router.post("/synthesis")
    def synthesis(request: SynthesisRequest, http_request: Request) -> dict[str, Any]:
        readiness = core.SYNTHESIS_BACKEND.warmup_status()
        if readiness["status"] in {"warming", "failed"}:
            raise HTTPException(status_code=409, detail=readiness["message"])
        config = _merge_config(request)
        # VoiceCard / StylePack overlay: explicit request fields always win over
        # the card's stored defaults, which in turn win over the saved config.
        if request.voice_card_id or request.style_id:
            try:
                if request.voice_card_id:
                    card = VOICE_CARDS.get(request.voice_card_id)
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
            prepared = apply_voice_selection(
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
            _record_api_key_chars(http_request, request.text)
            return _public_synthesis_snapshot(
                SYNTHESIS_JOBS.start(prepared.config, prepared.text)
            )
        except (RuntimeError, ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/synthesis/status/{job_id}")
    def synthesis_status(job_id: str) -> dict[str, Any]:
        snapshot = SYNTHESIS_JOBS.snapshot(job_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="合成任务不存在。")
        return _public_synthesis_snapshot(snapshot)

    @router.get("/synthesis/events/{job_id}")
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

    @router.get("/timbre/profile")
    def timbre_profile(profile_id: str = Query(default="current")) -> dict[str, Any]:
        """Report a voice profile's reference corpus, cache state and VRAM."""
        return TIMBRE_BACKEND.profile_status(profile_id, core.load_config())

    @router.post("/timbre/profile/build")
    def timbre_profile_build(request: TimbreProfileRequest) -> dict[str, Any]:
        """Encode a profile's reference audio into a cached kNN matching pool."""
        config = core.load_config()
        try:
            return _public_timbre_snapshot(
                TIMBRE_BACKEND.start_profile_build(request.profile_id, config, device=request.device)
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
            source = timbre.save_source(_upload_filename(audio, "source.wav"), audio.file)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail="源音频保存失败。") from exc
        config = core.load_config()
        return _public_timbre_snapshot(
            TIMBRE_BACKEND.start_conversion(source, profile_id, config, topk=topk, device=device)
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
            return _public_timbre_snapshot(
                TIMBRE_BACKEND.start_conversion(
                    source, request.profile_id, config, topk=request.topk, device=request.device
                )
            )
        except (RuntimeError, ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/timbre/status/{job_id}")
    def timbre_status(job_id: str) -> dict[str, Any]:
        snapshot = TIMBRE_BACKEND.snapshot(job_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="音色任务不存在。")
        return _public_timbre_snapshot(snapshot)

    @router.get("/timbre/jobs")
    def timbre_jobs(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
        return {"rows": [_public_timbre_snapshot(row) for row in TIMBRE_BACKEND.list_jobs(limit)]}

    @router.post("/timbre/unload")
    def timbre_unload() -> dict[str, Any]:
        """Release the encoder so synthesis can reclaim the GPU."""
        return TIMBRE_BACKEND.unload()

    @router.get("/history")
    def history() -> list[dict[str, Any]]:
        entries = core.load_history_with_downloads()
        for entry in entries:
            entry.pop("file", None)
            entry["url"] = _as_url(str(entry.get("download_id") or ""))
        return entries

    @router.delete("/history", status_code=204)
    def delete_history() -> Response:
        core.clear_history()
        return Response(status_code=204)

    @router.get("/files")
    def deprecated_file_query() -> None:
        raise HTTPException(status_code=404, detail="请使用下载链接。")

    @router.get("/files/{download_id}")
    def serve_file(download_id: str) -> FileResponse:
        candidate = core.resolve_download(download_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="下载文件不存在或已不可用。")
        return FileResponse(candidate, filename=candidate.name)

    @router.get("/training/status")
    def training_status() -> dict[str, Any]:
        config = core.load_config()
        return {"state": load_train_state(), "environment": train_env_rows(config), "stats": core.training_stats(config), "running": running_jobs(), "log": _current_log(), "last_job_id": last_job_id()}

    @router.get("/training/metrics")
    def training_metrics(job_id: str | None = None) -> dict[str, Any]:
        job_id = job_id or last_job_id()
        if not job_id:
            raise HTTPException(status_code=400, detail="还没有训练任务记录。")
        return {"rows": list_metrics(job_id)}

    @router.get("/training/checkpoints")
    def training_checkpoints() -> dict[str, Any]:
        return {"rows": list_checkpoint_entries()}

    @router.post("/training/dependencies")
    def install_dependencies() -> dict[str, Any]:
        missing = missing_deps()
        if not missing:
            return {"message": "训练依赖已齐全，无需安装。", "job_id": "", "log": _current_log()}
        job_id = start_deps_job(missing)
        return {"message": "正在后台安装：" + ", ".join(missing), "job_id": job_id, "log": _current_log(job_id)}

    @router.post("/training/patch")
    def training_patch() -> dict[str, Any]:
        config = core.load_config()
        root = _root_or_400(config)
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
        root = _root_or_400(config)
        if min_length < min_gap or min_gap < 10 or keep_silence < 10:
            raise HTTPException(status_code=400, detail="参数不满足约束：最短段长 >= 最短间隔 >= 10，保留静音 >= 10。")
        filename = Path(_upload_filename(recording, "recording.wav")).name
        raw = root / "custom_data" / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        destination = raw / filename
        with destination.open("wb") as output:
            shutil.copyfileobj(recording.file, output)
        job_id = start_job("slice", "录音切片", cmd_slice(str(root), silence_threshold, min_length, min_gap, keep_silence))
        return {"job_id": job_id, **_job_message("slice", f"切片任务已启动。录音已放入 {destination}。")}

    @router.post("/training/asr")
    def training_asr(request: ModelRunRequest) -> dict[str, Any]:
        config = core.load_config()
        root = _root_or_400(config)
        if not any((root / "custom_data" / "sliced").glob("*.wav")):
            raise HTTPException(status_code=400, detail="还没有切片数据，请先完成录音切片。")
        job_id = start_job("asr", "Whisper 识别", cmd_asr(str(root), model=request.model))
        return {"job_id": job_id, **_job_message("asr", f"识别任务已启动（模型 {request.model}）。")}

    @router.get("/training/transcript")
    def get_transcript() -> dict[str, Any]:
        root = _root_or_400(core.load_config())
        source = _transcript_path(root)
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
                row["audio_url"] = _as_url(core.register_download(audio_path))
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
        root = _root_or_400(core.load_config())
        output = root / "custom_data" / "asr" / "sliced_corrected.list"
        output.parent.mkdir(parents=True, exist_ok=True)
        result = write_corrected(output, request.rows)
        note = f"已保存 {result['lines']} 句"
        if result["sanitized"]:
            note += f"，其中 {result['sanitized']} 句含竖线已替换为全角"
        mark_step("correct", ST_DONE, note=note)
        return _job_message("correct", f"{note}到 sliced_corrected.list，可以进入特征提取。")

    @router.post("/training/transcript/skip")
    def skip_transcript() -> dict[str, Any]:
        root = _root_or_400(core.load_config())
        source = _transcript_path(root)
        if not source.exists():
            raise HTTPException(status_code=404, detail="还没有识别结果，请先完成 Whisper 识别。")
        destination = root / "custom_data" / "asr" / "sliced_corrected.list"
        shutil.copy2(source, destination)
        mark_step("correct", ST_DONE, note="直接使用识别结果")
        return _job_message("correct", "已直接使用识别结果。广普错字较多，建议仍抽时间校对。")

    @router.post("/training/features")
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

    @router.post("/training/run")
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

    @router.post("/training/stop")
    def training_stop(request: StopRequest) -> dict[str, Any]:
        job_id = request.job_id or last_job_id()
        if not job_id or not stop_job(job_id):
            raise HTTPException(status_code=400, detail="没有可停止的运行中任务。")
        return {"message": f"已请求停止任务 {job_id}", "log": _current_log(job_id)}

    @router.get("/training/log")
    def training_log(job_id: str | None = None, lines: int = Query(default=80, ge=1, le=500)) -> dict[str, str]:
        current = _current_log(job_id)
        if job_id and current["job_id"]:
            status = step_status(job_id.split("_")[0])
            path = status.get("log") or job_log_path(job_id)
            current["text"] = tail_log(path, lines) or current["text"]
        return current

    @router.post("/training/models/select")
    def select_models() -> dict[str, Any]:
        config = core.load_config()
        root = _root_or_400(config)
        models = find_models(str(root))
        if not models["gpt"] or not models["sovits"]:
            raise HTTPException(status_code=404, detail="还没找到训练产物，请先完成训练。")
        config.update(gpt_model=models["gpt"], sovits_model=models["sovits"])
        return {"message": "已填入并保存最新训练模型，可回合成页试音。", "config": core.save_config(config)}


    @app.exception_handler(media_ingest.MediaIngestError)
    async def media_ingest_error(_request, exc: media_ingest.MediaIngestError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"detail": str(exc), "code": "MEDIA_INGEST_FAILED"},
        )

    @app.exception_handler(GpuBusyTimeout)
    async def gpu_busy_error(_request, exc: GpuBusyTimeout) -> JSONResponse:
        """Another job owns the card: 409 with the holder named, not a 500."""
        return JSONResponse(
            status_code=409,
            content={"detail": str(exc), "code": "GPU_BUSY", "holder": exc.holder},
        )

    @app.exception_handler(StarletteHTTPException)
    async def api_error(_request, exc: StarletteHTTPException) -> JSONResponse:
        """Give every error a stable ``code`` next to the human ``detail``.

        The WebUI reads ``detail`` and keeps working; API clients can branch on
        ``code`` without parsing Chinese prose.
        """
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            return JSONResponse(status_code=exc.status_code, content=detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": detail if isinstance(detail, str) else str(detail),
                "code": _error_code(exc.status_code),
            },
        )

    @router.post("/media/sessions")
    def create_media_session(
        recording: UploadFile = File(...),
        name: str = Form(""),
        language: str = Form("zh"),
        rights_declared: bool = Form(False),
        rights_note: str = Form(""),
        transcribe: bool = Form(False),
        speaker_count: int | None = Form(default=None),
    ) -> dict[str, Any]:
        service = _media_service()
        session = service.create_session(
            name or Path(_upload_filename(recording, "media")).stem,
            language=language,
            rights_declared=rights_declared,
            rights_note=rights_note,
        )
        session = service.ingest_file(session["session_id"], _upload_filename(recording, "media"), recording.file)
        service.start_analysis(session["session_id"], speaker_count=speaker_count, transcribe=transcribe)
        return {
            "message": "媒体已上传，正在分析。",
            "session": _public_media_session(service, service.load(session["session_id"])),
        }

    @router.get("/media/sessions")
    def list_media_sessions() -> dict[str, Any]:
        service = _media_service()
        return {"rows": service.list_sessions()}

    @router.get("/media/sessions/{session_id}")
    def get_media_session(session_id: str) -> dict[str, Any]:
        service = _media_service()
        return {"session": _public_media_session(service, service.load(session_id))}

    @router.post("/media/sessions/{session_id}/analyze")
    def analyze_media_session(session_id: str, request: MediaAnalyzeRequest) -> dict[str, Any]:
        service = _media_service()
        service.start_analysis(
            session_id,
            speaker_count=request.speaker_count,
            transcribe=request.transcribe,
            model=request.model,
        )
        return {
            "message": "分析已启动。",
            "session": _public_media_session(service, service.load(session_id)),
        }

    @router.get("/media/sessions/{session_id}/job")
    def media_session_job(session_id: str) -> dict[str, Any]:
        service = _media_service()
        session = service.load(session_id)
        return {"job": session.get("job") or {}}

    @router.post("/media/sessions/{session_id}/select-speaker")
    def select_media_speaker(session_id: str, request: MediaSelectSpeakerRequest) -> dict[str, Any]:
        service = _media_service()
        session = service.select_speaker(session_id, request.speaker_id)
        return {
            "message": "已选择目标说话人。",
            "session": _public_media_session(service, session),
        }

    @router.post("/media/sessions/{session_id}/export")
    def export_media_dataset(session_id: str, request: MediaExportRequest) -> dict[str, Any]:
        service = _media_service()
        dataset = service.export_dataset(
            session_id,
            speaker_id=request.speaker_id,
            include_failed=request.include_failed,
            require_text=request.require_text,
            run_id=request.run_id,
        )
        public = _public_media_session(service, service.load(session_id))
        return {
            "message": "训练集已生成。",
            "dataset": public.get("dataset") or dataset,
            "session": public,
        }

    @router.post("/media/sessions/{session_id}/activate")
    def activate_media_dataset(session_id: str, request: MediaExportRequest) -> dict[str, Any]:
        service = _media_service()
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
        return {
            "message": f"已写入训练工作区：{result['copied']} 句。回到训练页即可做特征提取。",
            **result,
        }

    @router.get("/media/sessions/{session_id}/dataset/{dataset_id}/{filename}")
    def serve_media_dataset_file(session_id: str, dataset_id: str, filename: str) -> FileResponse:
        service = _media_service()
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
    app.include_router(router, prefix="/api")
    app.include_router(router, prefix="/v1")

    frontend_dist = Path(os.environ.get("XIANGHUA_FRONTEND_DIST", core.REPO_ROOT / "frontend" / "dist"))
    if frontend_dist.is_dir():
        assets = frontend_dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="frontend-assets")

        @app.get("/{frontend_path:path}", include_in_schema=False)
        def frontend(frontend_path: str) -> FileResponse:
            if frontend_path.startswith(("api/", "v1/")):
                # An unknown API path must be a JSON 404, never the SPA shell:
                # a client that mistypes a URL should not receive index.html.
                raise HTTPException(status_code=404, detail="接口不存在。")
            # 路径必须解析后仍落在 dist 内：百分号编码的 ../ 或绝对盘符
            # （例如 /%2e%2e/%2e%2e/backend/app.py）不允许读到 dist 外的文件。
            dist_root = frontend_dist.resolve()
            candidate = (frontend_dist / frontend_path).resolve()
            if frontend_path and candidate.is_relative_to(dist_root) and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(frontend_dist / "index.html")

    return app


app = create_app()
