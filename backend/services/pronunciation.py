"""Versioned pronunciation plans and staged accent rewriting.

This module is deliberately independent from the TTS engine.  It turns source text
into a :class:`PronunciationPlan` and never touches timbre, reference audio or model
weights.  The accent levels are cumulative, and protected compounds are matched as
real spans before replacement, so protection is enforced during rendering rather
than reported after the fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


class AccentLevel(str, Enum):
    """The only supported accent levels for the staged pronunciation layer."""

    OFF = "off"
    LIGHT = "light"
    MEDIUM = "medium"
    HEAVY = "heavy"

    def __str__(self) -> str:
        return self.value


LEXICON_ZH_BASE = "lex_zh_base_v1"

# Compounds whose characters may look like accent rules but whose standard
# pronunciation must survive every level, including heavy.
PROTECTED_TERMS: tuple[str, ...] = (
    "目的",
    "的确",
    "于是",
    "可是",
    "但是",
    "有的",
    "是的",
    "要是",
    "总是",
    "只是",
    "还是",
)

# Light: only phrases with an unambiguous regional equivalent.
LIGHT_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("是不是", "系唔系"),
    ("不是", "唔系"),
    ("没有", "冇"),
    ("什么人", "咩人"),
    ("最重要", "最紧要"),
)

# Medium: more colloquial word choices, but still no global 的/是 rewrite.
MEDIUM_REPLACEMENTS: tuple[tuple[str, str], ...] = LIGHT_REPLACEMENTS + (
    ("什么", "乜嘢"),
    ("没", "冇"),
    ("你说", "你话"),
    ("我说", "我话"),
)

# Heavy: the old to_lightmid semantics, now guarded by protected compounds.
HEAVY_REPLACEMENTS: tuple[tuple[str, str], ...] = MEDIUM_REPLACEMENTS + (
    ("的", "嘅"),
    ("是", "系"),
)

_ACCENT_INTENSITIES: dict[AccentLevel, float] = {
    AccentLevel.OFF: 0.0,
    AccentLevel.LIGHT: 0.35,
    AccentLevel.MEDIUM: 0.65,
    AccentLevel.HEAVY: 1.0,
}


def accent_intensity_for(accent_level: str | AccentLevel) -> float:
    """Public intensity lookup so asset layers (VoiceCard) can persist defaults."""
    return _ACCENT_INTENSITIES[_coerce_level(accent_level)]


@dataclass(frozen=True)
class PronunciationPlan:
    """The serialisable contract handed from text normalisation to the engine."""

    source_text: str
    rendered_text: str
    language: str
    lexicon_id: str
    accent_pack_id: str
    accent_intensity: float
    replacements: list[dict[str, Any]]
    protected_terms: list[str]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready copy of the plan."""
        return {
            "source_text": self.source_text,
            "rendered_text": self.rendered_text,
            "language": self.language,
            "lexicon_id": self.lexicon_id,
            "accent_pack_id": self.accent_pack_id,
            "accent_intensity": self.accent_intensity,
            "replacements": [dict(item) for item in self.replacements],
            "protected_terms": list(self.protected_terms),
        }


def _coerce_level(accent_level: str | AccentLevel) -> AccentLevel:
    if isinstance(accent_level, AccentLevel):
        return accent_level
    try:
        return AccentLevel(accent_level)
    except (TypeError, ValueError):
        allowed = ", ".join(level.value for level in AccentLevel)
        raise ValueError(f"accent_level 必须是以下之一：{allowed}") from None


def _is_chinese(language: str) -> bool:
    base = language.lower().replace("_", "-").split("-", 1)[0]
    return base == "zh"


def _rules_for(level: AccentLevel) -> tuple[tuple[str, str], ...]:
    if level is AccentLevel.OFF:
        return ()
    if level is AccentLevel.LIGHT:
        return LIGHT_REPLACEMENTS
    if level is AccentLevel.MEDIUM:
        return MEDIUM_REPLACEMENTS
    return HEAVY_REPLACEMENTS


def _render(
    source_text: str,
    rules: tuple[tuple[str, str], ...],
    user_rules: tuple[tuple[str, str], ...] = (),
) -> tuple[str, list[dict[str, Any]]]:
    """Render one pass over the original text using longest-match-first spans.

    ``user_rules``（用户发音词典）优先于口音规则与固定搭配保护：词典是用户的
    显式意图，例如为多音字或游戏专名指定 homophone 读法，必须原样生效。
    """
    protected = set(PROTECTED_TERMS)
    targets: dict[str, str | None] = {}
    for src, dst in user_rules:
        targets[src] = dst
    for term in PROTECTED_TERMS:
        targets.setdefault(term, None)
    for src, dst in rules:
        targets.setdefault(src, dst)
    user_sources = {src for src, _ in user_rules}

    # Priority order inside one length: user lexicon > protected compounds > accent
    # rules. Longest match still wins first, so a longer accent phrase can beat a
    # shorter user term — user rules are expected to be specific enough.
    entries: list[str] = list(user_sources) + list(PROTECTED_TERMS) + [src for src, _ in rules]
    entries.sort(key=lambda value: -len(value))
    pattern = re.compile("|".join(re.escape(value) for value in entries))

    parts: list[str] = []
    replacements: list[dict[str, Any]] = []
    cursor = 0
    for match in pattern.finditer(source_text):
        parts.append(source_text[cursor : match.start()])
        matched = match.group(0)
        target = targets[matched]
        if target is None:
            parts.append(matched)
        else:
            parts.append(target)
            replacements.append(
                {
                    "source": matched,
                    "replacement": target,
                    "start": match.start(),
                    "end": match.end(),
                    "origin": "user_lexicon" if matched in user_sources else "accent",
                }
            )
        cursor = match.end()
    parts.append(source_text[cursor:])
    return "".join(parts), replacements


def build_pronunciation_plan(
    text: str,
    language: str = "zh",
    accent_level: str | AccentLevel = "off",
    user_overrides: dict[str, str] | None = None,
) -> PronunciationPlan:
    """Build a pronunciation plan without consulting any target audio.

    Non-Chinese text passes through unchanged.  For Chinese, the selected level is
    cumulative: light adds unambiguous phrases, medium adds colloquial rules, and
    heavy adds the historical global 的/是 rules while still protecting fixed
    compounds.  ``user_overrides`` (the user lexicon) is applied on top of every
    level — including ``off`` — because an explicit reading correction is not an
    accent choice.
    """
    if not isinstance(text, str):
        raise TypeError("text 必须是字符串")

    level = _coerce_level(accent_level)
    language_text = str(language or "").strip()
    user_rules = tuple(
        (str(src), str(dst)) for src, dst in (user_overrides or {}).items() if src and dst
    )

    if not _is_chinese(language_text):
        return PronunciationPlan(
            source_text=text,
            rendered_text=text,
            language=language_text,
            lexicon_id="none",
            accent_pack_id="none",
            accent_intensity=0.0,
            replacements=[],
            protected_terms=[],
        )

    rules = _rules_for(level)
    if level is AccentLevel.OFF and not user_rules:
        rendered_text = text
        replacements: list[dict[str, Any]] = []
    else:
        rendered_text, replacements = _render(text, rules, user_rules)

    return PronunciationPlan(
        source_text=text,
        rendered_text=rendered_text,
        language=language_text,
        lexicon_id=LEXICON_ZH_BASE,
        accent_pack_id=f"guangpu_{level.value}",
        accent_intensity=_ACCENT_INTENSITIES[level],
        replacements=replacements,
        protected_terms=list(PROTECTED_TERMS),
    )