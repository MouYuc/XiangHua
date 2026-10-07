#!/usr/bin/env python3
"""Audit the DiaMoE dialect frontend for silent phoneme loss.

A character disappears from the synthesised audio without any error being
raised. Two independent defects cause that, and both are deterministic, so
neither needs a human listener to detect:

1. **Missing syllable entry.** ``hanzi.xlsx`` / ``word.xlsx`` / the tone-sandhi
   step emit a dialect pinyin syllable that ``syllable.xlsx`` does not contain.
   ``pinyin2ipa.py`` then falls back to ``p2i_dict.get(base, base)`` and passes
   the raw pinyin string through as if it were IPA. The acoustic model has no
   embedding for ``[zhe7]``, so ``ipa_to_model_text`` drops the token. The
   syllable is announced in the ``*.log`` file but the audio is simply shorter.

2. **Phoneme outside the model vocabulary.** The frontend produces legitimate
   IPA that ``diamoe_tts/data/vocab.txt`` has no bracket entry for. This one is
   not a frontend bug: it is a mismatch between the frontend inventory and the
   released acoustic model, and it can only be repaired by remapping the phone.

The probes in ``dialect_baseline.py`` only expose the gaps that happen to occur
in those four sentences. This script scans the *whole* lexicon instead, so the
backlog is complete rather than anecdotal.

Two details of the vendored frontend have to be reproduced exactly or the audit
reports noise instead of findings:

* ``syllable.xlsx`` stores ``initial`` and ``final`` as separate columns, and
  ``final`` may itself contain commas (``shou6`` -> initial ``ʃ``, final
  ``o̯,ˈʊ³¹``). The pipeline emits ``f"{initial},{final}"`` and later turns every
  comma into a space, so one syllable becomes several whitespace tokens and each
  token is looked up in the vocabulary separately.
* ``ipa_tone.py`` rewrites tone contours to ``ᴴ/ᴹ/ᴸ`` letters via a plain global
  string replace driven by ``tone.xlsx``. Comparing the digit form against the
  vocabulary would flag every toned nucleus as missing, so the same replacement
  has to run first.

``word.xlsx`` values are whole-word pinyin strings holding one syllable per
character (``阿姨`` -> ``a4 yi2``), so they are split before lookup.

Usage:
    python scripts/frontend_lexicon_audit.py
    python scripts/frontend_lexicon_audit.py --dialect shanghai
    python scripts/frontend_lexicon_audit.py --dump-syllables shanghai zhe
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from diamoe_runtime import load_symbol_tables  # noqa: E402

DEFAULT_FRONTEND_ROOT = Path("third_party/DiaMoE-TTS/dialect_frontend")
DEFAULT_DIAMOE_ROOT = Path("third_party/DiaMoE-TTS")

# Directories that hold a syllable table but are not part of the probe sweep.
NON_SWEEP_DIALECTS = {"jingjujingbai", "jingjuyunbai", "gaoxiong"}

_TONE_SUFFIX = re.compile(r"\d+$")
_HASH = "#"


def strip_tone(syllable: str) -> str:
    """``zhe7`` -> ``zhe``; leaves non-numeric suffixes untouched."""
    return _TONE_SUFFIX.sub("", syllable.strip())


def split_lexicon_pinyin(value: str) -> list[str]:
    """Split a lexicon pinyin value into individual syllables.

    ``word.xlsx`` collapses a whole word into one string, and the tone-sandhi
    step wraps connected speech in ``#...#``. Both have to be undone before a
    syllable can be looked up.
    """
    return [piece for piece in value.replace(_HASH, " ").split() if piece]


def load_tone_map(path: Path) -> list[tuple[str, str]]:
    """Return ordered (contour, mark) pairs exactly as ``ipa_tone.py`` reads them."""
    frame = pd.read_excel(path)
    pairs: list[tuple[str, str]] = []
    for _, row in frame.iterrows():
        contour = str(row["contour"]).strip()
        mark = str(row["mark"]).strip()
        if contour and contour.lower() != "nan":
            pairs.append((contour, mark))
    return pairs


def apply_tone(text: str, tone_map: list[tuple[str, str]]) -> str:
    """Replay the global contour-to-mark replacement of ``ipa_tone.py``."""
    for contour, mark in tone_map:
        text = text.replace(contour, mark)
    return text


def syllable_tokens(initial: str, final: str, tone_map: list[tuple[str, str]]) -> list[str]:
    """Reproduce the whitespace tokens the model actually receives for one syllable."""
    raw = f"{initial},{final}"
    raw = apply_tone(raw, tone_map)
    # The pipeline joins units, swaps every space for " | ", then every comma for
    # a space. A space inside an initial therefore becomes a literal pipe token,
    # which ipa_to_model_text skips.
    raw = raw.replace(" ", " | ").replace(",", " ")
    return [token for token in raw.split() if token != "|"]


def load_syllable_table(path: Path) -> dict[str, tuple[str, str]]:
    """Return pinyin -> (initial, final) for one dialect."""
    frame = pd.read_excel(path)
    table: dict[str, tuple[str, str]] = {}
    for _, row in frame.iterrows():
        pinyin = str(row["pinyin"]).strip()
        initial = "" if pd.isna(row["initial"]) else str(row["initial"]).strip()
        final = "" if pd.isna(row["final"]) else str(row["final"]).strip()
        table[pinyin] = (initial, final)
    return table


def load_lexicon_values(path: Path, is_word_table: bool) -> list[tuple[str, str]]:
    """Return (source_key, pinyin_value) pairs from a hanzi or word table."""
    frame = pd.read_excel(path)
    if is_word_table:
        key_column = "word" if "word" in frame.columns else frame.columns[0]
        value_columns = [c for c in frame.columns if "pinyin" in str(c).lower()]
        pairs: list[tuple[str, str]] = []
        for _, row in frame.iterrows():
            key = str(row[key_column]).strip()
            for column in value_columns:
                if pd.isna(row[column]):
                    continue
                value = str(row[column]).strip()
                if key and value and value.lower() != "nan":
                    pairs.append((key, value))
        return pairs

    pairs = []
    for _, row in frame.iterrows():
        key = str(row["hanzi"]).strip()
        value = "" if pd.isna(row["dialect_pinyin"]) else str(row["dialect_pinyin"]).strip()
        if key and value:
            pairs.append((key, value))
    return pairs


def audit_dialect(
    dialect: str,
    frontend_root: Path,
    ipa_set: set[str],
    dump_prefix: str | None = None,
) -> dict:
    """Measure both silent-loss defects for one dialect."""
    dialect_dir = frontend_root / "frontend" / "dialect" / dialect
    is_putonghua = dialect == "putonghua"
    syllable_path = dialect_dir / ("base_syllable.xlsx" if is_putonghua else "syllable.xlsx")
    tone_path = dialect_dir / ("base_tone.xlsx" if is_putonghua else "tone.xlsx")

    if not syllable_path.is_file():
        return {"dialect": dialect, "error": f"no syllable table at {syllable_path}"}

    tone_map = load_tone_map(tone_path) if tone_path.is_file() else []
    table = load_syllable_table(syllable_path)
    table_bases = {strip_tone(key) for key in table}

    if dump_prefix:
        for key in sorted(table):
            if key.startswith(dump_prefix):
                initial, final = table[key]
                tokens = syllable_tokens(initial, final, tone_map)
                bad = [t for t in tokens if f"[{t}]" not in ipa_set]
                flag = f"   <-- not in vocab: {bad}" if bad else ""
                print(f"    {key!r:14} initial={initial!r:10} final={final!r:16}{flag}")

    # --- defect 1: dialect pinyin the lexicon references but the syllable table
    # does not define, which makes pinyin2ipa.py emit raw pinyin as if it were IPA.
    referenced: dict[str, list[str]] = {}
    framing_errors: list[dict] = []
    for source, is_word in (
        ("hanzi.xlsx", False),
        ("word.xlsx", True),
        ("base_word.xlsx", True),
    ):
        path = dialect_dir / source
        if not path.is_file():
            continue
        for key, value in load_lexicon_values(path, is_word):
            syllables = split_lexicon_pinyin(value)
            # A word entry must supply exactly one syllable per spoken character.
            expected = len(key.replace(_HASH, ""))
            if is_word and len(syllables) != expected:
                framing_errors.append(
                    {"source": source, "key": key, "value": value,
                     "expected": expected, "got": len(syllables)}
                )
            for syllable in syllables:
                referenced.setdefault(syllable, []).append(f"{source}:{key}")

    missing_tone_variant: dict[str, list[str]] = {}
    missing_family: dict[str, list[str]] = {}
    for syllable, origins in referenced.items():
        if syllable in table:
            continue
        if strip_tone(syllable) in table_bases:
            missing_tone_variant[syllable] = origins
        else:
            missing_family[syllable] = origins

    # --- defect 2: IPA the frontend can emit that the acoustic model cannot read.
    vocab_missing: dict[str, list[str]] = {}
    for key, (initial, final) in table.items():
        for token in syllable_tokens(initial, final, tone_map):
            if f"[{token}]" not in ipa_set:
                vocab_missing.setdefault(token, []).append(key)

    referenced_families = sorted({strip_tone(s) for s in missing_family})
    return {
        "dialect": dialect,
        "syllable_entries": len(table),
        "lexicon_referenced_syllables": len(referenced),
        "missing_tone_variant": len(missing_tone_variant),
        "missing_family_syllables": len(missing_family),
        "missing_family_bases": referenced_families,
        "framing_errors": len(framing_errors),
        "vocab_missing_phones": len(vocab_missing),
        "missing_tone_variant_examples": dict(sorted(missing_tone_variant.items())[:20]),
        "missing_family_examples": dict(sorted(missing_family.items())[:20]),
        "missing_family_bases_all": referenced_families,
        "framing_error_examples": framing_errors[:10],
        "vocab_missing_phone_list": {
            phone: sorted(set(keys))[:6] for phone, keys in sorted(vocab_missing.items())
        },
    }


def load_hanzi_characters(path: Path) -> set[str]:
    """Return the unique characters a dialect's ``hanzi.xlsx`` actually defines."""
    if not path.is_file():
        return set()
    frame = pd.read_excel(path)
    return {str(value).strip() for value in frame["hanzi"] if str(value).strip()}


def inject_hanzi_coverage(reports: list[dict], dialect_root: Path) -> None:
    """Quantify defect A: under-populated ``hanzi.xlsx`` tables.

    A character missing from ``hanzi.xlsx`` is not reported anywhere: the
    frontend keeps the Mandarin pinyin, ``liandu_tone`` rewrites its tone number
    into the dialect's tone set, and the result is a syllable the dialect's
    ``syllable.xlsx`` has never heard of. ``pinyin2ipa.py`` then emits that raw
    pinyin string as if it were IPA and the model drops it.

    Scanning the lexicon cannot find this, because the failing characters are
    exactly the ones the lexicon does *not* mention. Comparing tables against
    each other can: a character defined by most dialects but missing from one is
    a common character that will silently fall back. This is what exposed
    Shanghai's 2842-character table against Nanjing's 6982.
    """
    character_sets: dict[str, set[str]] = {}
    for report in reports:
        if "error" in report:
            continue
        characters = load_hanzi_characters(
            dialect_root / report["dialect"] / "hanzi.xlsx"
        )
        # Putonghua ships no hanzi.xlsx on purpose: it needs no character
        # mapping, so an empty table there is correct and must not be read as a
        # gap, nor allowed to dilute the consensus.
        if not characters:
            report["hanzi_characters"] = 0
            report["missing_common_characters"] = 0
            report["missing_common_character_list"] = ""
            report["hanzi_note"] = "该方言表不含 hanzi.xlsx（无需字级映射）"
            continue
        character_sets[report["dialect"]] = characters
    if not character_sets:
        return

    counts: dict[str, int] = {}
    for characters in character_sets.values():
        for character in characters:
            counts[character] = counts.get(character, 0) + 1

    # A character carried by at least half the dialects is common enough that its
    # absence from one table is a defect rather than a dialect difference.
    threshold = max(2, len(character_sets) // 2)
    consensus = {character for character, seen in counts.items() if seen >= threshold}

    for report in reports:
        if "error" in report:
            continue
        if report["dialect"] not in character_sets:
            continue
        characters = character_sets[report["dialect"]]
        missing = consensus - characters
        report["hanzi_characters"] = len(characters)
        report["missing_common_characters"] = len(missing)
        report["missing_common_character_list"] = "".join(
            sorted(missing, key=lambda ch: (-counts[ch], ch))
        )[:120]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend-root", default=str(DEFAULT_FRONTEND_ROOT))
    parser.add_argument("--diamo-e-root", default=str(DEFAULT_DIAMOE_ROOT))
    parser.add_argument("--dialect", action="append", default=None)
    parser.add_argument(
        "--dump-syllables",
        nargs=2,
        metavar=("DIALECT", "PREFIX"),
        action="append",
        default=None,
        help="print syllable table rows whose pinyin starts with PREFIX",
    )
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args()

    frontend_root = Path(args.frontend_root)
    dialect_root = frontend_root / "frontend" / "dialect"
    ipa_set, _ = load_symbol_tables(Path(args.diamo_e_root))
    print(f"model vocabulary: {len(ipa_set)} bracket-wrapped IPA symbols")

    dialects = args.dialect or sorted(
        path.name
        for path in dialect_root.iterdir()
        if path.is_dir() and path.name not in NON_SWEEP_DIALECTS
    )

    dumps: dict[str, list[str]] = {}
    for dialect, prefix in args.dump_syllables or []:
        dumps.setdefault(dialect, []).append(prefix)

    reports = []
    for dialect in dialects:
        prefixes = dumps.get(dialect) or []
        for prefix in prefixes:
            print(f"===== {dialect} syllables starting with {prefix!r} =====")
            audit_dialect(dialect, frontend_root, ipa_set, dump_prefix=prefix)
        if not prefixes:
            reports.append(audit_dialect(dialect, frontend_root, ipa_set))

    inject_hanzi_coverage(reports, dialect_root)

    if reports:
        print()
        header = (
            f"{'dialect':<14}{'syll':>7}{'lex_syll':>9}{'tone_var':>9}"
            f"{'miss_fam':>9}{'bad_ph':>7}{'frame':>6}{'hanzi':>7}{'miss_ch':>8}"
        )
        print(header)
        print("-" * len(header))
        for report in reports:
            if "error" in report:
                print(f"{report['dialect']:<14}  {report['error']}")
                continue
            print(
                f"{report['dialect']:<14}"
                f"{report['syllable_entries']:>7}"
                f"{report['lexicon_referenced_syllables']:>9}"
                f"{report['missing_tone_variant']:>9}"
                f"{report['missing_family_syllables']:>9}"
                f"{report['vocab_missing_phones']:>7}"
                f"{report['framing_errors']:>6}"
                f"{report.get('hanzi_characters', 0):>7}"
                f"{report.get('missing_common_characters', 0):>8}"
            )
        print()
        print("tone_var = pinyin referenced by hanzi/word tables whose base syllable exists")
        print("           in syllable.xlsx but this tone variant does not (cheap to fill).")
        print("miss_fam = base syllable family absent from syllable.xlsx entirely.")
        print("bad_ph   = distinct IPA tokens reachable from syllable.xlsx that the model")
        print("           vocabulary has no embedding for.")
        print("frame    = word.xlsx rows whose syllable count does not match character count.")
        print("hanzi    = unique characters the dialect's hanzi.xlsx defines.")
        print("miss_ch  = common characters (carried by >= half the dialects) this table")
        print("           lacks; each one silently falls back to Mandarin pinyin.")

        for report in reports:
            if "error" in report:
                continue
            if not (
                report["missing_tone_variant"]
                or report["missing_family_syllables"]
                or report["vocab_missing_phones"]
                or report["framing_errors"]
            ):
                continue
            print()
            print(f"===== {report['dialect']} =====")
            if report["framing_error_examples"]:
                print("  framing errors:")
                for row in report["framing_error_examples"]:
                    print(
                        f"    {row['key']!r:16} -> {row['value']!r:22}"
                        f" (expected {row['expected']}, got {row['got']})"
                    )
            if report["missing_family_examples"]:
                print(f"  absent families ({len(report['missing_family_bases_all'])}): "
                      f"{', '.join(report['missing_family_bases_all'][:40])}")
                for syllable, origins in report["missing_family_examples"].items():
                    print(f"    {syllable!r:12} <- {', '.join(origins[:2])}")
            if report["missing_tone_variant_examples"]:
                print("  tone-variant gaps (base syllable exists):")
                for syllable, origins in report["missing_tone_variant_examples"].items():
                    print(f"    {syllable!r:12} <- {', '.join(origins[:2])}")
            if report["vocab_missing_phone_list"]:
                print("  phones absent from the model vocabulary:")
                for phone, keys in list(report["vocab_missing_phone_list"].items())[:30]:
                    print(f"    {phone!r:14} e.g. {keys[:4]}")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nwrote {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())