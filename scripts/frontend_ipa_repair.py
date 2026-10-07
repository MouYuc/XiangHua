#!/usr/bin/env python3
"""Turn Defect A's silent drops into audible Mandarin fallback syllables.

The defect
----------

``hanzi2dialect_pinyin.py`` falls back to the **Mandarin** pinyin whenever its
``hanzi.xlsx`` lookup misses::

    polyphonic_map.get((text_unit, pinyin_unit), simple_map.get(text_unit, pinyin_unit))

``liandu_tone.py`` then rewrites that syllable's tone number into the dialect's
tone set, and ``pinyin2ipa.py`` looks the result up in the *dialect* syllable
table, misses, and emits it verbatim::

    ipa = p2i_dict.get(base, base)

So ``这`` / ``zhe4`` leaves the frontend as the literal string ``zhe7``. The
acoustic model has no embedding for it, ``ipa_to_model_text`` drops the token, and
the character is simply not spoken. It is deterministic, it needs no listener to
detect, and it hits the most frequent function words - on the Shanghai table,
``这`` alone accounts for most of the loss.

What this tool does
-------------------

It does **not** invent a dialect reading. The dialect tables genuinely have no
entry for these syllables - ``zhan``, ``mei``, ``zhe``, ``wei`` and ``shen`` are
absent from Shanghai's ``syllable.xlsx`` entirely - so any "repair" that produced
dialect IPA would be fabrication.

Instead it uses data the vendor already ships: ``putonghua/base_syllable.xlsx``
is the Mandarin pinyin-to-IPA table, and ``putonghua/base_tone.xlsx`` is the
Mandarin contour-to-mark table. A syllable the dialect cannot pronounce is spoken
in Mandarin rather than not at all. That is worse for dialect authenticity but
strictly better for intelligibility, and it keeps the syllable count - and
therefore the timing budget F5 derives from the text length - intact.

Why a post-pass instead of a patch to the vendored scripts
----------------------------------------------------------

The Mandarin tone is destroyed by ``liandu_tone.py``'s sandhi rewriting, so the
repair needs the *pre-sandhi* reading. That reading is captured from stage 2, and
the substitution is then applied to the finished IPA. Keeping it here means no
vendored file is edited, the whole step is reversible, and the number of
substituted syllables is reported rather than guessed at.

Alignment
---------

``pinyin2ipa.format_ipa_output`` inserts a literal ``|`` between units before
turning each unit's internal commas into spaces, so the finished IPA splits on
``|`` into exactly one chunk per input character. A substitution is applied at
``(line, character index)`` and only when the chunk there is still a raw pinyin
string, so a line whose token count changed upstream is skipped and counted
instead of being silently mis-rewritten.

Usage:
    .venv\\Scripts\\python.exe scripts\\frontend_ipa_repair.py \\
        --work-dir output/frontend_loss_sweep_sent/work --dialect shanghai
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

DEFAULT_FRONTEND = REPO_ROOT / "third_party" / "DiaMoE-TTS" / "dialect_frontend"

# A raw pinyin token that reached the IPA stage unmapped. ``zhe7``, ``mei11``.
_PINYIN_LIKE = re.compile(r"^[A-Za-z]+\d*$")

# Punctuation is not a syllable and must never be touched.
_PUNCTUATION = set("，。？！、；：（）《》“”‘’—…·「」【】,.:;!?()[]")

# Marks used by the released tone tables and the tokenizer; kept separate from
# punctuation so a substituted syllable can never be mistaken for one.
_TONE_MARKS = "ᴴᴹᴸ"


def strip_tone(token: str) -> str:
    return re.sub(r"\d+$", "", str(token).strip())


# ``ipa_tone.py`` replaces each contour with its mark using a plain global string
# replace driven by ``tone.xlsx``. The same replacement has to be replayed here so
# a substituted syllable carries marks the vocabulary actually contains.
def load_tone_map(path: Path) -> list[tuple[str, str]]:
    if not path.is_file():
        return []
    frame = pd.read_excel(path)
    pairs: list[tuple[str, str]] = []
    for _, row in frame.iterrows():
        contour = str(row["contour"]).strip()
        mark = str(row["mark"]).strip()
        if contour and contour.lower() != "nan":
            pairs.append((contour, mark))
    return pairs


def apply_tone(text: str, tone_map: list[tuple[str, str]]) -> str:
    for contour, mark in tone_map:
        text = text.replace(contour, mark)
    return text


def load_syllable_table(path: Path) -> dict[str, tuple[str, str]]:
    frame = pd.read_excel(path)
    entries: dict[str, tuple[str, str]] = {}
    for _, row in frame.iterrows():
        pinyin = str(row["pinyin"]).strip()
        initial = "" if pd.isna(row["initial"]) else str(row["initial"]).strip()
        final = "" if pd.isna(row["final"]) else str(row["final"]).strip()
        if pinyin and pinyin.lower() != "nan":
            entries[pinyin] = (initial, final)
    return entries


class Mandarins:
    """The vendor's Mandarin pinyin-to-IPA and contour-to-mark tables."""

    def __init__(self, frontend_root: Path) -> None:
        base = frontend_root / "frontend" / "dialect" / "putonghua"
        self.syllables = load_syllable_table(base / "base_syllable.xlsx")
        self.tone_map = load_tone_map(base / "base_tone.xlsx")

    def ipa(self, pinyin: str) -> str | None:
        """Return the Mandarin IPA chunk for one toned pinyin syllable."""
        entry = self.syllables.get(pinyin)
        if entry is None:
            return None
        initial, final = entry
        chunk = f"{initial},{final}" if initial else final
        # Same two rewrites ``format_ipa_output`` applies inside the pipeline.
        chunk = chunk.replace(" ", " | ").replace(",", " ")
        return apply_tone(chunk, self.tone_map)


def read_rows(path: Path) -> dict[str, tuple[str, str]]:
    """Return ``id -> (text, payload)`` for a three-column stage file."""
    rows: dict[str, tuple[str, str]] = {}
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            rows[parts[0]] = (parts[1], parts[2])
    return rows


def find_fallbacks(
    stage2_path: Path,
    dialect_syllables: dict[str, tuple[str, str]],
) -> tuple[dict[str, dict[int, str]], dict]:
    """Locate the syllables the dialect table cannot pronounce.

    Read from stage 2 - after the character lookup, before tone sandhi - because
    that is the last point where the token still carries the Mandarin tone the
    Mandarin table is keyed by.
    """
    fallbacks: dict[str, dict[int, str]] = {}
    stats = {"lines": 0, "skipped_unaligned": 0, "tokens": 0, "fallback_tokens": 0}
    for row_id, (text, payload) in read_rows(stage2_path).items():
        characters = list(text)
        tokens = payload.split()
        stats["lines"] += 1
        stats["tokens"] += len(tokens)
        if len(tokens) != len(characters):
            stats["skipped_unaligned"] += 1
            continue
        per_line: dict[int, str] = {}
        for index, token in enumerate(tokens):
            bare = token.strip("#")
            if bare in dialect_syllables:
                continue
            if not _PINYIN_LIKE.match(bare):
                continue
            per_line[index] = bare
        if per_line:
            stats["fallback_tokens"] += len(per_line)
            fallbacks[row_id] = per_line
    return fallbacks, stats


def repair(
    final_path: Path,
    fallbacks: dict[str, dict[int, str]],
    mandarin: Mandarins,
) -> tuple[dict[str, tuple[str, str]], dict]:
    """Rewrite the finished IPA, one fallback syllable at a time."""
    rows = read_rows(final_path)
    report = {
        "lines": len(rows),
        "substituted": 0,
        "skipped_chunk_not_pinyin": 0,
        "skipped_line_length_changed": 0,
        "unmapped_by_mandarin_table": [],
        "substitutions": [],
        "by_character": {},
    }
    out: dict[str, tuple[str, str]] = {}
    for row_id, (text, payload) in rows.items():
        chunks = [chunk.strip() for chunk in payload.split("|")]
        per_line = fallbacks.get(row_id)
        if not per_line:
            out[row_id] = (text, payload)
            continue
        characters = list(text)
        if len(chunks) != len(characters):
            # A word-level replacement changed the token count, so an index no
            # longer means a character. Reported rather than repaired blindly.
            report["skipped_line_length_changed"] += 1
            out[row_id] = (text, payload)
            continue
        for index, pinyin in per_line.items():
            if index >= len(chunks):
                continue
            if not _PINYIN_LIKE.match(chunks[index]):
                # The chunk is no longer a raw pinyin token: either the pipeline
                # resolved it after all, or something else rewrote that position.
                report["skipped_chunk_not_pinyin"] += 1
                continue
            replacement = mandarin.ipa(pinyin)
            if replacement is None:
                if pinyin not in report["unmapped_by_mandarin_table"]:
                    report["unmapped_by_mandarin_table"].append(pinyin)
                continue
            report["substituted"] += 1
            character = characters[index]
            report["by_character"][character] = report["by_character"].get(character, 0) + 1
            if len(report["substitutions"]) < 200:
                report["substitutions"].append(
                    {
                        "line": row_id,
                        "index": index,
                        "character": character,
                        "was": chunks[index],
                        "pinyin": pinyin,
                        "now": replacement,
                    }
                )
            chunks[index] = replacement
        out[row_id] = (text, " | ".join(chunks))
    return out, report


def write_rows(path: Path, rows: dict[str, tuple[str, str]]) -> None:
    path.write_text(
        "\n".join(f"{row_id}\t{text}\t{payload}" for row_id, (text, payload) in rows.items()) + "\n",
        encoding="utf-8",
    )


def repair_dialect(
    dialect: str,
    work_dir: Path,
    frontend_root: Path,
    mandarin: Mandarins | None = None,
) -> dict:
    """Repair one dialect's finished IPA and write ``{dialect}_ipa_repaired.txt``."""
    if dialect == "putonghua":
        # Mandarin has no dialect table to miss, so there is nothing to fall back
        # from; the control dialect is returned untouched on purpose.
        return {"dialect": dialect, "skipped": "control dialect"}
    dialect_dir = frontend_root / "frontend" / "dialect" / dialect
    dialect_syllables = load_syllable_table(dialect_dir / "syllable.xlsx")
    mandarin = mandarin or Mandarins(frontend_root)

    stage2 = work_dir / f"{dialect}_hanzi.txt"
    final = work_dir / f"{dialect}_ipa_format.txt"
    if not stage2.is_file() or not final.is_file():
        return {"dialect": dialect, "error": f"missing stage output for {dialect}"}

    fallbacks, stats = find_fallbacks(stage2, dialect_syllables)
    rows, report = repair(final, fallbacks, mandarin)
    write_rows(work_dir / f"{dialect}_ipa_repaired.txt", rows)
    return {
        "dialect": dialect,
        "dialect_syllables": len(dialect_syllables),
        "characters_with_a_fallback": len(fallbacks),
        **stats,
        **report,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--work-dir", default=str(REPO_ROOT / "output" / "frontend_loss_sweep_sent" / "work"))
    parser.add_argument("--frontend-root", default=str(DEFAULT_FRONTEND))
    parser.add_argument("--dialect", action="append", default=None)
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args()

    work_dir = Path(args.work_dir)
    frontend_root = Path(args.frontend_root)
    dialect_root = frontend_root / "frontend" / "dialect"
    mandarin = Mandarins(frontend_root)
    print(f"Mandarin pinyin-to-IPA table: {len(mandarin.syllables)} syllables")

    dialects = args.dialect or sorted(
        path.name
        for path in dialect_root.iterdir()
        if path.is_dir() and (work_dir / f"{path.name}_hanzi.txt").is_file()
    )

    reports = []
    for dialect in dialects:
        report = repair_dialect(dialect, work_dir, frontend_root, mandarin)
        reports.append(report)
        if "error" in report:
            print(f"{dialect}: {report['error']}")
            continue
        if report.get("skipped"):
            print(f"{dialect}: {report['skipped']}")
            continue
        print(
            f"{dialect}: substituted {report['substituted']} syllables "
            f"across {report['characters_with_a_fallback']} lines"
        )
        if report["skipped_chunk_not_pinyin"]:
            print(f"    {report['skipped_chunk_not_pinyin']} positions were no longer raw pinyin")
        if report["skipped_line_length_changed"]:
            print(f"    {report['skipped_line_length_changed']} lines changed token count upstream")
        if report["unmapped_by_mandarin_table"]:
            print(f"    not in the Mandarin table either: {report['unmapped_by_mandarin_table'][:20]}")
        top = sorted(report["by_character"].items(), key=lambda item: -item[1])[:12]
        if top:
            print("    most frequent: " + ", ".join(f"{ch}x{n}" for ch, n in top))

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())