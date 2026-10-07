#!/usr/bin/env python3
"""Normalise frontend IPA into the acoustic model's vocabulary.

The DiaMoE frontend and the released acoustic model disagree about 64 IPA
tokens. ``ipa_to_model_text`` drops any token the vocabulary has no embedding
for, so those syllables silently vanish from the audio. This module repairs the
disagreement explicitly instead of dropping.

## Why the vocabulary disagrees

Measured facts, all reproduced by ``frontend_lexicon_audit.py``:

* The model vocabulary (``diamoe_tts/data/vocab.txt``, 440 bracket entries) is
  **internally consistent**: two-letter tone codes (``ᴴᴴ ᴴᴸ ᴴᴹ ᴸᴸ ᴸᴹ ᴹᴴ ᴹᴸ ᴹᴹ``)
  exist for every plain nucleus, while **single-letter tone codes belong to short
  (breve) vowels** — ``ˈăᴸ``, ``ˈĭᴸ``, ``ˈɛ̆ᴸ`` and so on.
* The frontend's ``syllable.xlsx`` tables are **less consistent**: several
  dialects write a checked-tone nucleus without the breve (``a12`` -> ``ˈaᴸ``)
  while writing the breve on the parallel ``ah*`` rows. That single missing
  diacritic is the whole of the largest failure class.

So the largest repair is not a substitution at all — it is restoring a diacritic
— and it preserves the tone code exactly.

## Rule order

Rules are tried in order and the first one that lands on a token **that actually
exists in the vocabulary** wins. A rule never invents a target: every candidate
is checked against the vocabulary, and anything still unresolved is reported
rather than guessed.

1. ``identity`` — already in the vocabulary.
2. ``strip_diacritic`` — the model never saw that diacritic on this segment.
3. ``add_breve`` — restore the short-vowel diacritic that carries single-letter
   tone codes. **Tone is preserved verbatim.**
4. ``expand_checked_tone`` — a single-letter code with no breve form available
   becomes the repeated level (``ᴸ`` -> ``ᴸᴸ``). This keeps the pitch *level*
   rather than shifting it, which a plain edit-distance choice would not.
5. ``nearest_contour`` — for an absent two-letter code, choose the present code
   with the smallest summed pitch distance (``ᴴ``/``ᴹ``/``ᴸ`` read as 2/1/0),
   breaking ties in favour of keeping the first letter, i.e. the tone's onset.
   This is deliberately the *last* tone rule because it is the only one that
   changes the tone, so it is logged for every token it touches.
6. ``substitution`` — a small, hand-reviewed table for segments the model simply
   does not carry. Each entry cites in-repository evidence; see ``SUBSTITUTIONS``.

The built table is written to ``assets/ipa_vocab_remap.json`` so that the runtime
only has to load JSON, and so that every mapping can be reviewed as data rather
than re-derived from rules at inference time.

Usage:
    python scripts/ipa_vocab_normalize.py --build     # regenerate the table
    python scripts/ipa_vocab_normalize.py --check     # fail if anything is unmapped
    python scripts/ipa_vocab_normalize.py --explain ɯ
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_VOCAB = Path("third_party/DiaMoE-TTS/diamoe_tts/data/vocab.txt")
DEFAULT_AUDIT = Path("output/frontend_lexicon_audit.json")
DEFAULT_REMAP = Path("assets/ipa_vocab_remap.json")

TONE_LETTERS = set("ᴴᴹᴸ")
_PITCH = {"ᴴ": 2, "ᴹ": 1, "ᴸ": 0}

# Diacritics that appear in the frontend tables but not on every vowel in the
# vocabulary. Order matters: the first one whose removal lands in the vocabulary
# is used.
DIACRITICS = ["̆", "̻", "̯", "̍", "̃", "̊", "̤", "̥", "̩", "̪", "̺", "̟", "̠"]

VOWELS = set("aeiouyøœæɐɑɒɔɛɘəɚɞɤɨɪɯʊʏɵɶ")

# Segments the model vocabulary does not carry in any form. Every entry needs
# in-repository evidence, because these are the only places where the frontend
# and the model genuinely use different symbols for the same sound.
SUBSTITUTIONS: dict[str, dict[str, str]] = {
    "ɯ": {
        "to": "ɹ̩",
        "reason": (
            "青岛用 ɯ 表示齿音 θ / t͜θ / t͜θʰ 后的舌尖元音（si/zi/ci，18 条）；"
            "青岛自己用 ɻ̍ 表示卷舌后的 zhi/chi/shi/ri。"
            "其余八个方言表对同一批 si/zi/ci 全部写 ɹ̩"
            "（如上海 ci1 -> t͜sʰ,ˈɹ̩⁵²），故 ɯ 与 ɹ̩ 是同一音的两套记法。"
        ),
    },
    "ɨ": {
        "to": "i",
        "reason": (
            "武汉只有 10 条用到 ɨ，全部是零声母音节 ii1..ii5（即普通话 yi）；"
            "武汉自己用 ɹ̩ 表示 si 等舌尖元音。其它方言把同一音节写作 i，"
            "而词表同时具备 i 与 ɹ̩，故选 i。"
        ),
    },
    "ɘ": {
        "to": "ə",
        "reason": (
            "青岛 204 条用 ɘ（en 等），词表已有 ˈɘᴴᴴ/ˈɘᴴᴸ/ˈɘᴸᴸ 等带调形式，"
            "仅缺无调形式；青岛的声调 5 不产生调符，故需要无调 ɘ。"
            "词表无无调 ɘ，最近的无调央元音是 ə。"
        ),
    },
    "t͜θʰ": {
        "to": "t͜sʰ",
        "reason": (
            "青岛 408 条用 t͜θʰ，与 t͜θ 构成送气对立（草 /t͜θʰ/ 对 早 /t͜θ/）。"
            "词表有 t͜θ 与 t͜sʰ，没有 t͜θʰ。"
            "映射到 t͜sʰ 同时保住塞擦与送气；若映射到 t͜θ 则送气丢失，"
            "会把草与早合并成同一个音，所以不取最小编辑距离的那条。"
        ),
    },
}


def load_vocabulary(path: Path = DEFAULT_VOCAB) -> set[str]:
    """Return the bracket-wrapped IPA entries without their brackets."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return {line[1:-1] for line in lines if line.startswith("[") and line.endswith("]")}


def split_tone(token: str) -> tuple[str, str]:
    """Split a token into (stress+nucleus, tone letters)."""
    index = len(token)
    while index > 0 and token[index - 1] in TONE_LETTERS:
        index -= 1
    return token[:index], token[index:]


def pitch_distance(left: str, right: str) -> int:
    """Summed level distance between two tone codes, padding the shorter one.

    A single-letter code is a short realization of that level, so it is compared
    against the corresponding letter of the longer code.
    """
    length = max(len(left), len(right))
    padded_left = (left * length)[:length] if len(left) < length else left
    padded_right = (right * length)[:length] if len(right) < length else right
    return sum(
        abs(_PITCH.get(a, 1) - _PITCH.get(b, 1))
        for a, b in zip(padded_left, padded_right)
    )


def tone_inventory(vocabulary: set[str]) -> dict[str, set[str]]:
    """Map each stress+nucleus to the set of tone codes the vocabulary has for it."""
    inventory: dict[str, set[str]] = defaultdict(set)
    for token in vocabulary:
        nucleus, tone = split_tone(token)
        if tone:
            inventory[nucleus].add(tone)
    return inventory


def _rule_strip_diacritic(token: str, vocabulary: set[str]) -> tuple[str, str] | None:
    for diacritic in DIACRITICS:
        if diacritic in token:
            candidate = token.replace(diacritic, "")
            if candidate in vocabulary:
                return candidate, "strip_diacritic"
    return None


def _rule_add_breve(token: str, vocabulary: set[str]) -> tuple[str, str] | None:
    nucleus, tone = split_tone(token)
    for index, char in enumerate(nucleus):
        if char in VOWELS:
            candidate = f"{nucleus[:index + 1]}̆{nucleus[index + 1:]}{tone}"
            if candidate in vocabulary:
                return candidate, "add_breve"
    return None


def _rule_expand_checked_tone(token: str, vocabulary: set[str]) -> tuple[str, str] | None:
    nucleus, tone = split_tone(token)
    if len(tone) != 1:
        return None
    candidate = nucleus + tone * 2
    if candidate in vocabulary:
        return candidate, "expand_checked_tone"
    return None


def _rule_nearest_contour(
    token: str, vocabulary: set[str], inventory: dict[str, set[str]]
) -> tuple[str, str] | None:
    nucleus, tone = split_tone(token)
    if not tone:
        return None
    available = inventory.get(nucleus)
    if not available:
        return None
    best = min(
        available,
        key=lambda mark: (
            pitch_distance(tone, mark),
            # Keep the tone's onset when the distance ties.
            0 if mark[:1] == tone[:1] else 1,
            mark,
        ),
    )
    return nucleus + best, "nearest_contour"


def _rule_substitution(token: str, vocabulary: set[str]) -> tuple[str, str] | None:
    nucleus, tone = split_tone(token)
    for source, entry in SUBSTITUTIONS.items():
        if source not in nucleus:
            continue
        candidate = nucleus.replace(source, entry["to"]) + tone
        if candidate in vocabulary:
            return candidate, "substitution"
    return None


RULES = (
    _rule_strip_diacritic,
    _rule_add_breve,
    _rule_expand_checked_tone,
    _rule_nearest_contour,
    _rule_substitution,
)


def map_token(
    token: str, vocabulary: set[str], inventory: dict[str, set[str]]
) -> tuple[str, str] | None:
    """Return (replacement, rule) or None when nothing defensible applies."""
    if token in vocabulary:
        return token, "identity"
    for rule in RULES:
        if rule is _rule_nearest_contour:
            result = rule(token, vocabulary, inventory)
        else:
            result = rule(token, vocabulary)
        if result:
            return result
    return None


def collect_reachable_tokens(frontend_root: Path) -> dict[str, set[str]]:
    """Every token the frontend can emit, read from the audit report.

    The audit already enumerates the tokens the model has no embedding for; this
    module consumes that report so both tools agree on the problem statement.
    """
    audit_path = DEFAULT_AUDIT
    if not audit_path.is_file():
        raise SystemExit(
            f"missing {audit_path}; run scripts/frontend_lexicon_audit.py --json-out {audit_path} first"
        )
    reports = json.loads(audit_path.read_text(encoding="utf-8"))
    by_dialect: dict[str, set[str]] = {}
    for report in reports:
        phones = report.get("vocab_missing_phone_list")
        if phones:
            by_dialect[report["dialect"]] = set(phones)
    return by_dialect


def build_remap(
    vocabulary: set[str], by_dialect: dict[str, set[str]]
) -> tuple[dict[str, dict], list[str], list[str]]:
    """Build the remap table, plus the unresolved and sequence-handled reports."""
    inventory = tone_inventory(vocabulary)
    table: dict[str, dict] = {}
    unresolved: list[str] = []
    sequence_handled: list[str] = []

    for dialect, tokens in sorted(by_dialect.items()):
        for token in sorted(tokens):
            result = map_token(token, vocabulary, inventory)
            if result is None:
                # A toned glide coda needs its neighbour to be repaired, so no
                # token-to-token entry can express it; record it separately after
                # checking that the glide itself has an embedding.
                if is_toned_glide(token):
                    bare = token[: -len(split_tone(token)[1])]
                    if bare in vocabulary:
                        sequence_handled.append(f"{dialect}:{token}")
                        continue
                unresolved.append(f"{dialect}:{token}")
                continue
            replacement, rule = result
            if replacement == token:
                continue
            entry = table.setdefault(
                token,
                {"to": replacement, "rule": rule, "dialects": [], "reason": ""},
            )
            if dialect not in entry["dialects"]:
                entry["dialects"].append(dialect)

    for token, entry in table.items():
        if entry["rule"] == "substitution":
            source = next(
                (s for s in SUBSTITUTIONS if s in split_tone(token)[0]), None
            )
            entry["reason"] = SUBSTITUTIONS[source]["reason"] if source else ""
        else:
            entry["reason"] = RULE_NOTES[entry["rule"]]
    return table, unresolved, sequence_handled


RULE_NOTES = {
    "strip_diacritic": (
        "模型词表在该音段上没有这个附加符；去掉后即为词表内条目，音值与声调均不变。"
    ),
    "add_breve": (
        "词表中单字母调符只出现在短元音（带短音符 ̆）上，而前端部分音节漏写了短音符。"
        "补回短音符后落入词表，**声调记号原样保留**。"
    ),
    "expand_checked_tone": (
        "单字母调符是该调阶的短调实现，词表对该韵母没有单字母形式。"
        "重复该字母得到同一调阶的完整形式，**保住调阶**，而不是改成别的调。"
    ),
    "nearest_contour": (
        "该韵母在词表中缺少这个双字母调符。按 H/M/L 三个调阶的距离取最近者，"
        "距离相同时保留首字母（即调型的起点）。"
        "**这是唯一会改变声调的规则，命中项全部记录在此供复核。**"
    ),
    "glide_tone_move": (
        "前端表中该音节的调号挂在了非成音节的滑音尾上，而同音节的其它调、"
        "以及它的儿化形都把调号挂在韵母上，属表内笔误。"
        "此处把调号搬回韵母，**声调不变**，只是换了承载位置。"
    ),
}

# Non-syllabic glide codas. A tone code here is a placement artifact: the tone
# belongs to the syllable nucleus.
GLIDE_DIACRITIC = "̯"


def is_toned_glide(token: str) -> bool:
    """True when a tone code sits on a non-syllabic glide coda."""
    nucleus, tone = split_tone(token)
    return bool(tone) and nucleus.endswith(GLIDE_DIACRITIC)


def repair_tone_on_glide(
    tokens: list[str], vocabulary: set[str]
) -> tuple[list[str], list[dict[str, str]]]:
    """Move a tone code from a glide coda back onto its bare stressed nucleus.

    Qingdao's ``syllable.xlsx`` writes ``bei6`` as ``ˈe,ɪ̯³¹`` while ``bei1`` is
    ``ˈe¹¹³,ɪ̯`` and the erhua form ``beir6`` is ``ˈɚ³¹`` — the tone digits simply
    landed on the wrong segment for tone 6. Left alone, the nucleus keeps no tone
    and the toned glide has no embedding, so the syllable loses its tone entirely.

    The move is only performed when both resulting tokens exist in the
    vocabulary, so it can never fabricate an unsupported pair.
    """
    repaired = list(tokens)
    trace: list[dict[str, str]] = []
    for index in range(1, len(repaired)):
        glide = repaired[index]
        nucleus = repaired[index - 1]
        if not is_toned_glide(glide):
            continue
        _, tone = split_tone(glide)
        bare_glide = glide[: -len(tone)]
        # The carrier must be a stressed nucleus that currently has no tone.
        if not nucleus.startswith("ˈ") or split_tone(nucleus)[1]:
            continue
        moved = nucleus + tone
        if moved not in vocabulary or bare_glide not in vocabulary:
            continue
        repaired[index - 1] = moved
        repaired[index] = bare_glide
        trace.append(
            {
                "from": f"{nucleus} + {glide}",
                "to": f"{moved} + {bare_glide}",
                "rule": "glide_tone_move",
            }
        )
    return repaired, trace


def load_remap(path: Path = DEFAULT_REMAP) -> dict[str, str]:
    """Load the token-to-token mapping used at inference time."""
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        token: str(entry["to"])
        for token, entry in (payload.get("mappings") or {}).items()
    }


def normalize_tokens(
    tokens: list[str], vocabulary: set[str], remap: dict[str, str]
) -> tuple[list[str], list[dict[str, str]]]:
    """Apply the sequence repair and the token remap, returning a full trace.

    Every change is recorded. A token that is neither in the vocabulary nor in
    the remap is passed through unchanged and reported by ``unmapped_tokens``,
    so callers can surface it instead of losing it silently.
    """
    tokens, trace = repair_tone_on_glide(tokens, vocabulary)
    mapped: list[str] = []
    for token in tokens:
        if token in vocabulary:
            mapped.append(token)
            continue
        replacement = remap.get(token)
        if replacement is None:
            mapped.append(token)
            trace.append({"from": token, "to": token, "rule": "unmapped"})
            continue
        mapped.append(replacement)
        trace.append({"from": token, "to": replacement, "rule": "remapped"})
    return mapped, trace


def unmapped_tokens(
    tokens: list[str], vocabulary: set[str], remap: dict[str, str]
) -> list[str]:
    """Tokens that would still be dropped for lack of an embedding."""
    tokens, _ = repair_tone_on_glide(tokens, vocabulary)
    return [
        token
        for token in tokens
        if token not in vocabulary and token not in remap
    ]


# Punctuation the model's ``punctuation.txt`` and ``vocab.txt`` both lack, so
# ``ipa_to_model_text`` drops it before the tokenizer ever sees it. Only the two
# marks that actually carry a pause are aliased; brackets and quotation marks are
# dropped on purpose, because they delimit text rather than signal a break, and
# the model has no comparable symbol to spend on them.
#
#   ； -> ，   a semicolon separates clauses, which is exactly what the comma the
#             model does have expresses as prosody
#   ： -> ，   a colon announces what follows; the model's only comparable pause
#
# Everything in this table survives into the model text, so the aliased form is
# what the acoustic model reads.
PUNCTUATION_ALIASES: dict[str, str] = {
    "；": "，",
    "：": "，",
}

# Dropped by ``ipa_to_model_text`` and deliberately left alone. Recorded so the
# behaviour is a decision rather than an accident, and so a test can assert it.
PUNCTUATION_DROPPED_ON_PURPOSE = set("（）【】()[]“”‘’")


def apply_punctuation_aliases(tokens: list[str]) -> tuple[list[str], list[dict[str, str]]]:
    """Map pause-carrying punctuation the model lacks onto one it has."""
    mapped: list[str] = []
    trace: list[dict[str, str]] = []
    for token in tokens:
        replacement = PUNCTUATION_ALIASES.get(token)
        if replacement is None:
            mapped.append(token)
            continue
        mapped.append(replacement)
        trace.append({"from": token, "to": replacement, "rule": "punctuation_alias"})
    return mapped, trace


def normalize_for_model(
    tokens: list[str], vocabulary: set[str], remap: dict[str, str]
) -> tuple[list[str], list[dict[str, str]]]:
    """The single normalisation entry point: punctuation aliases, then symbols.

    Both the production path (``diamoe_runtime.ipa_to_model_text``) and the
    measuring scripts call this, so a change to the rules cannot move one of them
    without the other.
    """
    tokens, punctuation_trace = apply_punctuation_aliases(tokens)
    tokens, trace = normalize_tokens(tokens, vocabulary, remap)
    return tokens, punctuation_trace + trace


def write_remap(path: Path, table: dict[str, dict], extra: dict) -> None:
    payload = {
        "version": 1,
        "description": (
            "前端 IPA -> 模型 IPA 归一化表。由 scripts/ipa_vocab_normalize.py --build 生成；"
            "规则与逐条理由见该脚本与 docs/DIALECT_LEXICON_DEFECTS.md。"
            "运行时只读本文件，不再推导规则。"
        ),
        "mappings": dict(sorted(table.items())),
        **extra,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vocab", default=str(DEFAULT_VOCAB))
    parser.add_argument("--remap", default=str(DEFAULT_REMAP))
    parser.add_argument("--build", action="store_true", help="regenerate the remap table")
    parser.add_argument("--check", action="store_true", help="fail if any token is unmapped")
    parser.add_argument("--explain", default=None, help="explain how one token is mapped")
    parser.add_argument("--frontend-root", default="third_party/DiaMoE-TTS/dialect_frontend")
    args = parser.parse_args()

    vocabulary = load_vocabulary(Path(args.vocab))
    inventory = tone_inventory(vocabulary)
    print(f"model vocabulary: {len(vocabulary)} IPA tokens")

    if args.explain:
        result = map_token(args.explain, vocabulary, inventory)
        if result is None:
            print(f"{args.explain!r}: 无法映射（词表内无可用目标）")
            return 1
        replacement, rule = result
        print(f"{args.explain!r} -> {replacement!r}  [{rule}]")
        print(f"  理由: {RULE_NOTES.get(rule, SUBSTITUTIONS.get(args.explain, {}).get('reason', ''))}")
        if rule == "nearest_contour":
            nucleus, tone = split_tone(args.explain)
            print(f"  该韵母 {nucleus!r} 词表内可用调符: {sorted(inventory.get(nucleus, []))}")
        return 0

    by_dialect = collect_reachable_tokens(Path(args.frontend_root))
    table, unresolved, sequence_handled = build_remap(vocabulary, by_dialect)

    by_rule = Counter(entry["rule"] for entry in table.values())
    print(f"frontend dialects with gaps: {len(by_dialect)}")
    print(f"mapped tokens: {len(table)}  by rule: {dict(by_rule)}")
    print(f"sequence-handled tokens (tone moved off a glide coda): {len(sequence_handled)}")
    for item in sequence_handled:
        print(f"  SEQUENCE {item}")
    print(f"unresolved tokens: {len(unresolved)}")
    for item in unresolved:
        print(f"  UNRESOLVED {item}")

    if args.build:
        write_remap(
            Path(args.remap),
            table,
            {
                "vocabulary_size": len(vocabulary),
                "unresolved": unresolved,
                "sequence_handled": sorted(set(sequence_handled)),
                "rule_notes": RULE_NOTES,
                "substitutions": SUBSTITUTIONS,
            },
        )
        print(f"wrote {args.remap}")

    if args.check:
        if unresolved:
            print("FAIL: unresolved tokens would still be dropped by the model")
            return 1
        # A stale table must not pass: every mapped token has to land in the vocabulary.
        for token, entry in table.items():
            if entry["to"] not in vocabulary:
                print(f"FAIL: {token!r} maps to {entry['to']!r} which is not in the vocabulary")
                return 1
        # The saved table must match what the rules currently produce.
        saved = load_remap(Path(args.remap))
        expected = {token: entry["to"] for token, entry in table.items()}
        if saved != expected:
            print(
                f"FAIL: {args.remap} is stale "
                f"({len(saved)} entries saved vs {len(expected)} generated); rerun --build"
            )
            return 1
        # The sequence rule must be executable for every token it claims.
        for item in sequence_handled:
            token = item.split(":", 1)[1]
            bare = token[: -len(split_tone(token)[1])]
            if bare not in vocabulary:
                print(f"FAIL: glide coda {token!r} has no bare form {bare!r} in the vocabulary")
                return 1
        print("OK: every reachable frontend token has an in-vocabulary target")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())