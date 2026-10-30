"""Request models for the local XiangHua API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

SynthesisLanguage = Literal["zh", "yue", "en", "ja", "ko"]
AccentLevel = Literal["off", "light", "medium", "heavy"]


class ConfigUpdate(BaseModel):
    gpt_sovits_root: str | None = None
    gpt_model: str | None = None
    sovits_model: str | None = None
    ref_audio: str | None = None
    ref_text: str | None = None
    text_lang: SynthesisLanguage | None = None
    prompt_lang: SynthesisLanguage | None = None
    top_k: int | None = Field(default=None, ge=1)
    top_p: float | None = Field(default=None, gt=0, le=1)
    temperature: float | None = Field(default=None, gt=0, le=2)
    sample_steps: int | None = Field(default=None, ge=1)
    speed: float | None = Field(default=None, gt=0, le=3)
    pause_second: float | None = Field(default=None, ge=0, le=10)
    voice_profile_id: str | None = None
    accent_id: str | None = None
    accent_level: AccentLevel | None = None
    take_selection: bool | None = None
    mixed_eos_floor: int | None = Field(default=None, ge=0)
    mixed_token_cap: int | None = Field(default=None, ge=0)
    auto_lightmid: bool | None = None
    theme: str | None = None
    orb_synthesis_token_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    orb_settled_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    orb_playback_token_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")

    @field_validator(
        "orb_synthesis_token_color",
        "orb_settled_color",
        "orb_playback_token_color",
        mode="before",
    )
    @classmethod
    def normalize_orb_color(cls, value: str | None) -> str | None:
        return str(value).upper() if value is not None else None


class VoiceCardCreate(BaseModel):
    display_name: str = Field(min_length=1)
    language: SynthesisLanguage = "zh"
    license_id: str | None = None
    consent_id: str | None = None
    notes: str | None = None


class VoiceCardUpdate(BaseModel):
    display_name: str | None = None
    status: Literal["active", "retired"] | None = None
    license_id: str | None = None
    consent_id: str | None = None
    active_version: str | None = None


class VoiceCardVersionCreate(BaseModel):
    notes: str | None = None


class SynthesisRequest(ConfigUpdate):
    text: str
    voice_card_id: str | None = None
    style_id: str | None = None


class EnvironmentSelectRequest(BaseModel):
    path: str


class TranscriptUpdate(BaseModel):
    rows: list[dict[str, Any]]


class ModelRunRequest(BaseModel):
    model: str = "large-v3"


class FeatureRequest(BaseModel):
    experiment: str = "myvoice"


class TrainRequest(FeatureRequest):
    s1_epochs: int = Field(default=15, ge=1)
    s2_epochs: int = Field(default=12, ge=1)


class StopRequest(BaseModel):
    job_id: str | None = None


class TimbreConvertRequest(BaseModel):
    """Convert an audio file that is already on disk into a profile's timbre.

    The WebUI uses this to move a synthesis result into the user's own voice
    without downloading and re-uploading it. Exactly one of ``source_path`` or
    ``download_id`` has to resolve to a readable file.
    """

    source_path: str | None = None
    download_id: str | None = None
    profile_id: str = "current"
    topk: int = Field(default=4, ge=1, le=64)
    device: str | None = None


class TimbreProfileRequest(BaseModel):
    profile_id: str = "current"
    device: str | None = None


class ReferenceAudioRequest(BaseModel):
    """Grade a reference clip, or a whole reference directory, before synthesis.

    ``directory`` exists because the two consumers judge different units: a
    synthesis prompt is one file, while a kNN-VC matching pool is the whole corpus,
    and 64 two-second slices each fail a per-file minimum that the corpus as a whole
    passes comfortably.
    """

    path: str | None = None
    directory: str | None = None
    purpose: str = "synthesis"
    ref_text: str | None = None


class DoctorRequest(BaseModel):
    repair: bool = False

class MediaAnalyzeRequest(BaseModel):
    """Start speaker diarization for an uploaded media session."""

    speaker_count: int | None = Field(default=None, ge=1, le=12)
    transcribe: bool = False
    model: str = "large-v3"


class MediaSelectSpeakerRequest(BaseModel):
    speaker_id: str


class MediaExportRequest(BaseModel):
    """Target one speaker's slices, optionally pinned to a past run or dataset.

    ``dataset_id`` re-activates an already exported dataset (its manifest is the
    snapshot), while ``run_id`` selects one analysis run of the session. Without
    either, the session's active run is used.
    """

    speaker_id: str | None = None
    include_failed: bool = False
    require_text: bool = True
    dataset_id: str | None = None
    run_id: str | None = None


class LexiconUpsertRequest(BaseModel):
    """One user pronunciation dictionary entry (term -> homophone reading)."""

    term: str
    replacement: str
    note: str = ""
    enabled: bool = True


class BatchExportRequest(BaseModel):
    """Export one finished synthesis job as an episode package."""

    job_id: str


class DialogueSynthesisRequest(BaseModel):
    """Synthesise a ``名字：台词`` script, one voice card per role.

    ``roles`` maps speaker names to voice card ids; speakers without a binding
    (including 旁白) use the current saved synthesis config.
    """

    script: str
    roles: dict[str, str] = Field(default_factory=dict)


class DialogueExportRequest(BaseModel):
    """Export one finished dialogue job as an episode package with role stems."""

    job_id: str


class ProvenanceVerifyRequest(BaseModel):
    """Verify a signed provenance sidecar against its audio file.

    Exactly one of ``path`` (confined to the repository) or ``download_id``
    should identify the audio file.
    """

    path: str | None = None
    download_id: str | None = None


class ApiKeyCreateRequest(BaseModel):
    """Create one tenant API key; the token is returned exactly once."""

    tenant: str = Field(min_length=1)
    label: str = ""
    daily_request_quota: int = Field(default=2000, ge=0)
    rpm_limit: int = Field(default=30, ge=0)
