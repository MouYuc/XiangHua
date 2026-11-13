"""Voice profile and regional accent selection for synthesis requests."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.domain.text import SUPPORTED_SYNTHESIS_LANGUAGES
from backend.infra.paths import CONFIG_DIR, REPO_ROOT

VOICE_PROFILE_FILE = CONFIG_DIR / "voice_profiles.json"
ACCENT_MANIFEST_FILE = REPO_ROOT / "assets" / "accent_packs.json"
ACCENTS_ROOT = REPO_ROOT / "assets" / "accent_packs"
_VOICE_TAG_BODY_PATTERN = re.compile(
    r"(<(?:zh|yue)>)(.*?)(</(?:zh|yue)>)", re.IGNORECASE | re.DOTALL
)


@dataclass(frozen=True)
class VoiceProfile:
    id: str
    display_name: str
    description: str
    gpt_model: str
    sovits_model: str
    ref_audio: str
    ref_text: str
    prompt_lang: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "description": self.description,
        }


@dataclass(frozen=True)
class AccentPack:
    id: str
    display_name: str
    lang: str
    description: str
    uses_profile_reference: bool
    ref_audio: str
    ref_text: str
    prompt_lang: str
    pronunciation_lexicon: dict[str, str]
    sentence_templates: dict[str, str]
    enabled: bool
    available: bool

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "lang": self.lang,
            "description": self.description,
            "enabled": self.enabled,
            "available": self.available,
        }


@dataclass(frozen=True)
class PreparedSelection:
    config: dict[str, Any]
    text: str
    profile: VoiceProfile
    accent: AccentPack


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"无法读取配置文件：{path}") from exc
    except ValueError as exc:
        raise ValueError(f"配置文件不是有效 JSON：{path}") from exc


def _resolve_path(value: str, base: Path) -> str:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return str(candidate)


def _current_profile(config: dict[str, Any]) -> VoiceProfile:
    return VoiceProfile(
        id="current",
        display_name="当前音色",
        description="使用设置页或环境变量里的模型与参考音频。",
        gpt_model=str(config.get("gpt_model") or ""),
        sovits_model=str(config.get("sovits_model") or ""),
        ref_audio=str(config.get("ref_audio") or ""),
        ref_text=str(config.get("ref_text") or ""),
        prompt_lang=str(config.get("prompt_lang") or "zh"),
    )


def load_voice_profiles(
    config: dict[str, Any], profile_file: Path | None = None
) -> list[VoiceProfile]:
    profiles = [_current_profile(config)]
    source = profile_file or VOICE_PROFILE_FILE
    if not source.is_file():
        return profiles

    payload = _read_json(source)
    if not isinstance(payload, dict) or not isinstance(payload.get("profiles"), list):
        raise ValueError("voice_profiles.json 必须包含 profiles 数组。")
    for raw in payload["profiles"]:
        if not isinstance(raw, dict):
            raise ValueError("voice_profiles.json 的 profiles 成员必须是对象。")
        profile = VoiceProfile(
            id=str(raw.get("id") or "").strip(),
            display_name=str(raw.get("display_name") or "").strip(),
            description=str(raw.get("description") or "").strip(),
            gpt_model=str(raw.get("gpt_model") or "").strip(),
            sovits_model=str(raw.get("sovits_model") or "").strip(),
            ref_audio=str(raw.get("ref_audio") or "").strip(),
            ref_text=str(raw.get("ref_text") or "").strip(),
            prompt_lang=str(raw.get("prompt_lang") or "zh").strip(),
        )
        if not profile.id or not profile.display_name:
            raise ValueError("声音档案必须包含 id 和 display_name。")
        existing_ids = {profile.id for profile in profiles}
        if profile.id in {"current", *existing_ids}:
            raise ValueError("声音档案 id 不能是 current 或重复 id。")
        if profile.prompt_lang not in SUPPORTED_SYNTHESIS_LANGUAGES:
            raise ValueError(f"声音档案语言不支持：{profile.prompt_lang}")
        if not all(
            [profile.gpt_model, profile.sovits_model, profile.ref_audio, profile.ref_text]
        ):
            raise ValueError(f"声音档案缺少模型或参考音频：{profile.id}")
        profiles.append(profile)
    return profiles


def load_accent_packs(
    manifest_path: Path | None = None, assets_root: Path | None = None
) -> list[AccentPack]:
    source = manifest_path or ACCENT_MANIFEST_FILE
    root = assets_root or ACCENTS_ROOT
    payload = _read_json(source)
    if not isinstance(payload, dict) or not isinstance(payload.get("packs"), list):
        raise ValueError("accent_packs.json 必须包含 packs 数组。")

    packs: list[AccentPack] = []
    seen_ids: set[str] = set()
    for raw in payload["packs"]:
        if not isinstance(raw, dict):
            raise ValueError("accent_packs.json 的 packs 成员必须是对象。")
        accent_id = str(raw.get("id") or "").strip()
        if not accent_id or accent_id in seen_ids:
            raise ValueError("口音包 id 不能为空或重复。")
        seen_ids.add(accent_id)
        language = str(raw.get("lang") or "zh").strip()
        if language not in SUPPORTED_SYNTHESIS_LANGUAGES:
            raise ValueError(f"口音包语言不支持：{language}")
        uses_profile_reference = bool(raw.get("uses_profile_reference"))
        ref_audio = str(raw.get("ref_audio") or "").strip()
        ref_text = str(raw.get("ref_text") or "").strip()
        prompt_lang = str(raw.get("prompt_lang") or language).strip()
        if prompt_lang not in SUPPORTED_SYNTHESIS_LANGUAGES:
            raise ValueError(f"口音包参考语言不支持：{prompt_lang}")
        if not uses_profile_reference and (not ref_audio or not ref_text):
            raise ValueError(f"口音包缺少参考音频或文本：{accent_id}")
        resolved_audio = (
            "" if uses_profile_reference else _resolve_path(ref_audio, root)
        )
        packs.append(
            AccentPack(
                id=accent_id,
                display_name=str(raw.get("display_name") or accent_id).strip(),
                lang=language,
                description=str(raw.get("description") or "").strip(),
                uses_profile_reference=uses_profile_reference,
                ref_audio=resolved_audio,
                ref_text=ref_text,
                prompt_lang=prompt_lang,
                pronunciation_lexicon={
                    str(key): str(value)
                    for key, value in (raw.get("pronunciation_lexicon") or {}).items()
                },
                sentence_templates={
                    str(key): str(value)
                    for key, value in (raw.get("sentence_templates") or {}).items()
                },
                enabled=bool(raw.get("enabled", True)),
                available=uses_profile_reference or Path(resolved_audio).is_file(),
            )
        )
    return packs


def resolve_voice_profile(
    config: dict[str, Any],
    profile_id: str | None = None,
    profile_file: Path | None = None,
) -> VoiceProfile:
    profiles = load_voice_profiles(config, profile_file)
    selected = profile_id or str(config.get("voice_profile_id") or "current")
    by_id = {profile.id: profile for profile in profiles}
    if selected not in by_id:
        raise ValueError(f"声音档案不存在：{selected}")
    return by_id[selected]


def resolve_accent_pack(
    accent_id: str | None = None,
    fallback_id: str | None = None,
    manifest_path: Path | None = None,
    assets_root: Path | None = None,
) -> AccentPack:
    selected = accent_id or fallback_id or "standard"
    packs = load_accent_packs(manifest_path, assets_root)
    by_id = {pack.id: pack for pack in packs}
    if selected not in by_id:
        raise ValueError(f"口音包不存在：{selected}")
    accent = by_id[selected]
    if not accent.enabled or not accent.available:
        raise ValueError(f"口音包不可用：{accent.display_name}")
    return accent


def _rewrite_plain_line(text: str, accent: AccentPack) -> str:
    stripped = text.strip()
    if stripped and stripped in accent.sentence_templates:
        return text.replace(stripped, accent.sentence_templates[stripped])
    for source in sorted(accent.pronunciation_lexicon, key=len, reverse=True):
        target = accent.pronunciation_lexicon[source]
        if source:
            text = text.replace(source, target)
    return text


def rewrite_text_for_accent(text: str, accent: AccentPack | None) -> str:
    if accent is None or (
        not accent.pronunciation_lexicon and not accent.sentence_templates
    ):
        return text
    if _VOICE_TAG_BODY_PATTERN.search(text):
        return _VOICE_TAG_BODY_PATTERN.sub(
            lambda match: match.group(1)
            + _rewrite_plain_line(match.group(2), accent)
            + match.group(3),
            text,
        )
    return "\n".join(
        _rewrite_plain_line(line, accent) for line in text.splitlines(keepends=True)
    )


def apply_voice_selection(
    config: dict[str, Any],
    text: str,
    voice_profile_id: str | None = None,
    accent_id: str | None = None,
    profile_file: Path | None = None,
    manifest_path: Path | None = None,
    assets_root: Path | None = None,
) -> PreparedSelection:
    profile = resolve_voice_profile(config, voice_profile_id, profile_file)
    accent = resolve_accent_pack(
        accent_id,
        str(config.get("accent_id") or "standard"),
        manifest_path,
        assets_root,
    )
    selected_config = dict(config)
    selected_config["voice_profile_id"] = profile.id
    selected_config["accent_id"] = accent.id
    for key in ("gpt_model", "sovits_model", "ref_audio", "ref_text", "prompt_lang"):
        value = getattr(profile, key)
        if value:
            selected_config[key] = value
    if not accent.uses_profile_reference:
        selected_config["ref_audio"] = accent.ref_audio
        selected_config["ref_text"] = accent.ref_text
        selected_config["prompt_lang"] = accent.prompt_lang
    return PreparedSelection(
        config=selected_config,
        text=rewrite_text_for_accent(text, accent),
        profile=profile,
        accent=accent,
    )


def list_voice_options(
    config: dict[str, Any],
    profile_file: Path | None = None,
    manifest_path: Path | None = None,
    assets_root: Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    return {
        "profiles": [
            profile.public_dict()
            for profile in load_voice_profiles(config, profile_file)
        ],
        "accents": [
            accent.public_dict()
            for accent in load_accent_packs(manifest_path, assets_root)
        ],
    }
