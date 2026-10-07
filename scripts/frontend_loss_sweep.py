#!/usr/bin/env python3
"""Measure silent character loss by running the *real* dialect frontend.

``frontend_lexicon_audit.py`` reads the vendored ``.xlsx`` tables directly, which
is fast but blind in two places:

* ``hanzi2dialect_pinyin.py`` falls back to the **Mandarin** pinyin for any
  character its ``hanzi.xlsx`` does not define, and ``liandu_tone.py`` then
  rewrites that syllable's tone number into the dialect's tone set. The result is
  a syllable the dialect's ``syllable.xlsx`` has never heard of, which appears in
  no table, so a table scan cannot enumerate it.
* Whether the Mandarin fallback is *audible* or *silent* depends on whether the
  dialect's own inventory happens to contain that syllable. Only running the
  pipeline answers that.

So this script runs the vendored six-stage pipeline and asks three questions.

1. **Did the vendor report the leak itself?** ``pinyin2ipa.py`` already writes a
   ``.log`` beside its output listing every pinyin item it could not map
   ("Summary of pinyin mapping items not found"). Nothing reads that file today,
   so the frontend has been diagnosing its own silent failures all along.
2. **Did the character survive into model tokens?** A one-character-per-line
   corpus keeps a strict 1:1 alignment, so the tokens the acoustic model receives
   can be attributed back to a single character. A character whose line yields no
   readable token is a character that is not spoken.
3. **Does the normalisation table rescue it?** Tokens are looked up with and
   without ``assets/ipa_vocab_remap.json``, separating the loss the remap already
   repairs from the loss that still needs a decision.

Two deliberate design choices keep the numbers honest.

**The corpus is per dialect, and its Mandarin reading is the one the dialect
table itself expects.** Feeding a context-free Mandarin G2P's *preferred* reading
would blame the dialect lexicon for polyphone selection: Shanghai's table lists
惟-style 占 only under ``zhan1``, so the preferred ``zhan4`` "leaks", but the
table is not missing the character. Using the table's own reading measures what
the pipeline does when the Mandarin reading is the best case. Polyphone exposure -
characters the table supports only under a reading a Mandarin G2P would not
usually pick - is a property of the tables, so it is counted separately, straight
from the tables, without a pipeline run.

**Step 0 of the upstream pipeline is not used.** ``gen_ppinyin_oop.py`` needs a
g2pW ONNX model the vendored repository does not ship - it looks for the literal
placeholder path ``path/to/g2pW_model/g2pw.onnx``. Mandarin pinyin therefore comes
from ``pypinyin`` (tone-number style, neutral tone as ``5``), and the reading is
then overridden by the dialect table wherever the table states one. Characters
``pypinyin`` has no reading for at all are excluded and counted, because
``pypinyin`` hands rare CJK extensions straight back (``㓕5``) and that would be
scored as a lost character.

A one-character line cannot trigger word-level mapping or tone sandhi, so this
measures the character path only. That is exactly the path Defect A lives on.

Usage:
    .venv\\Scripts\\python.exe scripts\\frontend_loss_sweep.py --build-corpus
    .venv\\Scripts\\python.exe scripts\\frontend_loss_sweep.py
    .venv\\Scripts\\python.exe scripts\\frontend_loss_sweep.py --dialect shanghai --limit 500
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ipa_vocab_normalize as normalizer  # noqa: E402
from diamoe_runtime import load_symbol_tables  # noqa: E402
from frontend_ipa_repair import Mandarins, repair, find_fallbacks, write_rows  # noqa: E402

DEFAULT_ROOT = REPO_ROOT / "third_party" / "DiaMoE-TTS"
DEFAULT_FRONTEND = DEFAULT_ROOT / "dialect_frontend"
DIALECT_ROOT_NAME = "frontend/dialect"

# Dialects whose tables exist but which are not part of the product's dialect set.
SKIPPED_DIALECTS = {"jingjujingbai", "jingjuyunbai", "gaoxiong"}

# Punctuation reaches the "not found" log for every dialect and is expected:
# ``pinyin2ipa.py`` only knows syllables, and punctuation is carried to the model
# as punctuation. Counted separately so it cannot inflate the leak count.
PUNCTUATION = set("，。？！、；：（）《》“”‘’—…·「」【】")

_LEAK_HEADER = "not found"
_LEAK_ITEM = re.compile(r"^\s*-\s*(\S+)\s*$")
_TONE_SUFFIX = re.compile(r"\d+$")

# Matches the vendored driver's own rule: only the erhua fixer takes no dialect.
STAGES_WITHOUT_DIALECT = {"fix_erhua.py"}

# ``zhe7``, ``mei11``: a dropped token that is still a bare Latin string is the
# signature of Defect A - the character's Mandarin pinyin travelled all the way to
# ``pinyin2ipa.py`` and was emitted as though it were IPA. Anything else that was
# dropped is a phoneme-inventory mismatch, which is a different repair.
_PINYIN_LIKE = re.compile(r"^[A-Za-z]+\d*$")

# A real Mandarin reading is letters plus one tone digit (``zhe4``, ``nv3``).
# ``pypinyin`` returns the character itself when it has no reading at all.
_PINYIN_READING = re.compile(r"^[A-Za-züÜ]+[1-5]$")


def strip_tone(syllable: str) -> str:
    return _TONE_SUFFIX.sub("", str(syllable).strip())


# --------------------------------------------------------------------- tables ---


def load_hanzi_table(path: Path) -> pd.DataFrame:
    return pd.read_excel(path)


def load_hanzi_map(path: Path) -> tuple[set[str], dict[str, set[str]]]:
    """Reproduce ``hanzi2dialect_pinyin.load_mapping``'s view of one dialect table.

    Returns ``(unconditional, conditional)``: characters the table maps regardless
    of the Mandarin reading, and characters it maps only for specific readings.
    """
    frame = load_hanzi_table(path)
    unconditional: set[str] = set()
    conditional: dict[str, set[str]] = {}
    for _, row in frame.iterrows():
        character = str(row["hanzi"]).strip()
        if not character:
            continue
        standard = row["standard_chinese_pinyin"]
        standard = "" if pd.isna(standard) else str(standard).strip()
        if not standard:
            unconditional.add(character)
            continue
        readings = {piece.strip() for piece in standard.split(",") if piece.strip()}
        conditional.setdefault(character, set()).update(readings)
    return unconditional, conditional


def load_syllable_entries(path: Path) -> dict[str, tuple[str, str]]:
    """Return ``pinyin -> (initial, final)`` for one dialect."""
    frame = pd.read_excel(path)
    entries: dict[str, tuple[str, str]] = {}
    for _, row in frame.iterrows():
        pinyin = str(row["pinyin"]).strip()
        initial = "" if pd.isna(row["initial"]) else str(row["initial"]).strip()
        final = "" if pd.isna(row["final"]) else str(row["final"]).strip()
        if pinyin and pinyin.lower() != "nan":
            entries[pinyin] = (initial, final)
    return entries


def consensus_characters(dialect_root: Path, threshold_ratio: float = 0.5) -> list[str]:
    """Return the characters most dialects define, ordered by how many do.

    The count doubles as a frequency proxy: a character eight of the nine dialects
    tabulated is a common one, and losing it is a defect rather than a dialect
    difference. ``putonghua`` is excluded - it ships no ``hanzi.xlsx`` by design.
    """
    counts: Counter[str] = Counter()
    dialects = 0
    for directory in sorted(dialect_root.iterdir()):
        if not directory.is_dir() or directory.name in SKIPPED_DIALECTS:
            continue
        path = directory / "hanzi.xlsx"
        if not path.is_file():
            continue
        frame = load_hanzi_table(path)
        dialects += 1
        counts.update({str(value).strip() for value in frame["hanzi"] if str(value).strip()})

    threshold = max(2, int(dialects * threshold_ratio))
    return [
        character
        for character, seen in sorted(
            ((character, seen) for character, seen in counts.items() if seen >= threshold),
            key=lambda item: (-item[1], item[0]),
        )
    ]


def mandarin_pinyin(text: str) -> list[str]:
    """Mandarin pinyin in the vendor's tone-number style (neutral tone = 5)."""
    from pypinyin import Style, lazy_pinyin

    return lazy_pinyin(text, style=Style.TONE3, neutral_tone_with_five=True)


# --------------------------------------------------------------------- corpus ---


def dialect_readings(character: str, unconditional: set[str], conditional: dict[str, set[str]]) -> list[str]:
    """The Mandarin readings this dialect's table expects for one character.

    An empty return means the table does not mention the character, so the caller
    falls back to the Mandarin G2P. A single empty string means the table maps the
    character regardless of reading, so any Mandarin reading resolves it.
    """
    if character in unconditional:
        return [""]
    readings = conditional.get(character)
    if readings is None:
        return []
    return sorted(readings)


# Han characters plus the punctuation the frontend carries through. A sentence
# built only from these maps one pinyin token per character, which is what
# ``hanzi2dialect_pinyin.py`` asserts; Latin letters or digits would collapse
# several characters into one token and break the alignment.
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_SENTENCE_PUNCTUATION = set("，。？！、；：（）《》“”‘’—…·「」【】")
_SENTENCE_SPLIT = re.compile(r"[。！？；\n]+")


def is_sentence_safe(text: str) -> bool:
    return all(_HAN.match(character) or character in _SENTENCE_PUNCTUATION for character in text)


def build_sentence_corpus(sources: list[Path], path: Path, limit: int, min_chars: int, max_chars: int) -> dict:
    """Write an ``id<TAB>sentence<TAB>pinyin`` corpus from Chinese prose.

    Real sentences are what expose the defect the character corpus cannot: only in
    context does the Mandarin G2P pick the reading a sentence actually intends, and
    a polyphone the dialect table lists under a different reading then leaks. The
    prose used is the repository's own documentation, so the corpus is
    reproducible and needs no download.
    """
    from pypinyin import Style, lazy_pinyin

    seen: set[str] = set()
    rows: list[tuple[str, str]] = []
    skipped = 0
    for source in sources:
        if not source.is_file():
            continue
        for raw in _SENTENCE_SPLIT.split(source.read_text(encoding="utf-8", errors="replace")):
            sentence = raw.strip()
            if not (min_chars <= len(sentence) <= max_chars):
                continue
            if not is_sentence_safe(sentence) or sentence in seen:
                continue
            if len(sentence) < min_chars:
                continue
            seen.add(sentence)
            rows.append((sentence, " ".join(lazy_pinyin(sentence, style=Style.TONE3, neutral_tone_with_five=True))))
            if limit and len(rows) >= limit:
                break
        if limit and len(rows) >= limit:
            break

    # Alignment is an assertion inside the vendored step 2, so verify it here
    # rather than letting a malformed corpus fail the run halfway.
    aligned: list[tuple[str, str]] = []
    for sentence, pinyin in rows:
        if len(pinyin.split()) == len(sentence):
            aligned.append((sentence, pinyin))
        else:
            skipped += 1

    path.write_text(
        "\n".join(f"s{n:05d}\t{text}\t{pinyin}" for n, (text, pinyin) in enumerate(aligned)) + "\n",
        encoding="utf-8",
    )
    return {
        "sentences": len(aligned),
        "skipped_misaligned": skipped,
        "mean_characters": round(sum(len(text) for text, _ in aligned) / len(aligned), 1) if aligned else 0.0,
    }


def write_dialect_corpus(
    characters: list[str],
    unconditional: set[str],
    conditional: dict[str, set[str]],
    path: Path,
    limit: int = 0,
) -> tuple[dict, list[str]]:
    """Write an ``id<TAB>text<TAB>pinyin`` corpus for one dialect.

    Returns the summary and the generated pinyin for each line, so a leak can be
    attributed without re-reading the file.
    """
    lines: list[str] = []
    rows: list[list[str]] = []
    skipped_unalignable = 0
    skipped_no_reading = 0
    from_table_reading = 0
    unconditional_reading = 0
    g2p_reading = 0

    for character in characters:
        chosen: list[str] = []
        table = dialect_readings(character, unconditional, conditional)
        if table == [""]:
            # The table maps this character for any reading; the Mandarin pinyin
            # only matters for the polyphone lookup, which will hit either way.
            unconditional_reading += 1
            table = []
        if table:
            chosen = table
            from_table_reading += 1
        else:
            candidates = mandarin_pinyin(character)
            if len(candidates) != 1:
                # Zero or several syllables cannot be aligned against a single
                # text unit, and the vendored step 2 asserts on that mismatch.
                skipped_unalignable += 1
                continue
            if not _PINYIN_READING.match(candidates[0]):
                # ``pypinyin`` has no reading for rare CJK extensions and hands
                # the character back; scoring that would blame the frontend.
                skipped_no_reading += 1
                continue
            chosen = [candidates[0]]
            g2p_reading += 1

        # A character the table maps for several readings is probed once per
        # reading: the dialect value can differ, so only testing one hides a leak.
        for reading in chosen:
            index = f"c{len(rows):05d}"
            lines.append(f"{index}\t{character}\t{reading}")
            rows.append([index, character, reading])
            if limit and len(rows) >= limit:
                break
        if limit and len(rows) >= limit:
            break

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return (
        {
            "lines": len(rows),
            "from_table_reading": from_table_reading,
            "unconditional_reading": unconditional_reading,
            "from_g2p_reading": g2p_reading,
            "skipped_unalignable": skipped_unalignable,
            "skipped_no_mandarin_reading": skipped_no_reading,
        },
        [row[2] for row in rows],
    )


def polyphone_exposure(
    characters: list[str], conditional: dict[str, set[str]]
) -> dict:
    """Count characters a context-free Mandarin G2P would read the "wrong" way.

    These leak whenever the sentence intends the common reading, even though the
    table does define the character. It is a table property, so it is measured
    from the table plus the G2P and needs no pipeline run. Reported separately
    because the fix is different: the character entry exists and only the
    reading list is short.
    """
    exposed: list[dict] = []
    for character in characters:
        readings = conditional.get(character)
        if not readings:
            continue
        candidates = mandarin_pinyin(character)
        if len(candidates) != 1 or not _PINYIN_READING.match(candidates[0]):
            continue
        preferred = candidates[0]
        if preferred not in readings:
            exposed.append(
                {"character": character, "preferred": preferred, "table_reads": sorted(readings)}
            )
    return {
        "polyphone_exposed_characters": len(exposed),
        "polyphone_examples": exposed[:40],
    }


# ------------------------------------------------------------------- pipeline ---


def run_stage(
    script: str,
    input_path: Path,
    output_path: Path,
    frontend_root: Path,
    dialect: str,
    python_exe: str,
) -> subprocess.CompletedProcess:
    """Run one vendored stage; the scripts resolve their xlsx tables from cwd."""
    command = [python_exe, script, "-i", str(input_path.resolve()), "-o", str(output_path.resolve())]
    if script not in STAGES_WITHOUT_DIALECT:
        command += ["-d", dialect]
    return subprocess.run(
        command,
        cwd=str(frontend_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


FRONTEND_STEPS = [
    ("fix_erhua.py", "fix"),
    ("hanzi2dialect_pinyin.py", "hanzi"),
    ("word2dialect_pinyin.py", "word"),
    ("liandu_tone.py", "liandu"),
    ("pinyin2ipa.py", "ipa"),
    ("ipa_tone.py", "ipa_format"),
]


def run_pipeline(dialect: str, corpus: Path, work_dir: Path, frontend_root: Path, python_exe: str) -> dict:
    """Run the vendored dialect pipeline for one dialect."""
    work_dir.mkdir(parents=True, exist_ok=True)
    current = work_dir / f"{dialect}_00_pinyin.txt"
    current.write_text(corpus.read_text(encoding="utf-8"), encoding="utf-8")

    log: list[dict] = []
    for script, tag in FRONTEND_STEPS:
        if dialect == "putonghua" and script in {"fix_erhua.py", "hanzi2dialect_pinyin.py"}:
            # Putonghua has no character mapping step by design; the vendored
            # driver skips these two, so skipping them here keeps the comparison
            # against the same number of transformations as the other dialects.
            continue
        output = work_dir / f"{dialect}_{tag}.txt"
        completed = run_stage(script, current, output, frontend_root, dialect, python_exe)
        log.append(
            {
                "script": script,
                "returncode": completed.returncode,
                "stderr_tail": completed.stderr.strip()[-1500:],
            }
        )
        if completed.returncode != 0 or not output.is_file():
            return {"ok": False, "stage": script, "log": log}
        current = output
    return {"ok": True, "final": current, "log": log}


def read_leak_log(path: Path) -> list[str]:
    """Parse ``pinyin2ipa``'s "not found" summary into the leaked item list."""
    if not path.is_file():
        return []
    leaks: list[str] = []
    collecting = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if _LEAK_HEADER in line:
            collecting = True
            continue
        if not collecting:
            continue
        match = _LEAK_ITEM.match(line)
        if match:
            leaks.append(match.group(1))
        elif line.strip():
            collecting = False
    return leaks


def read_stage_rows(path: Path) -> dict[str, str]:
    """Return ``id -> payload`` (third column) for a stage output file."""
    rows: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            rows[parts[0]] = parts[2]
    return rows


# -------------------------------------------------------------------- scoring ---


def normalizer_out(
    raw_ipa: str,
    vocabulary: set[str],
    remap: dict[str, str],
    punctuation: set[str],
) -> tuple[list[str], list[str]]:
    """Apply the same pipeline ``ipa_to_model_text`` does, on bare symbols.

    Punctuation counts as kept, exactly as ``ipa_to_model_text`` treats it: the
    model carries ``，`` and ``。`` through as themselves, so scoring them as
    dropped would report every sentence break as a lost character.
    """
    tokens, _ = normalizer.normalize_for_model(raw_ipa.split(), vocabulary, remap)
    kept = [token for token in tokens if token in vocabulary or token in punctuation]
    dropped = [
        token for token in tokens
        if token not in vocabulary and token not in punctuation and token != "|"
    ]
    return kept, dropped


def classify_leak(
    character: str,
    pinyin: str,
    unconditional: set[str],
    conditional: dict[str, set[str]],
    syllable_entries: dict[str, tuple[str, str]],
    syllable_bases: set[str],
) -> str:
    """Attribute one raw-pinyin leak to its actual cause.

    ``hanzi2dialect_pinyin.py`` emits the Mandarin pinyin whenever its lookup
    misses, so a leak says only "the lookup missed". The misses need different
    fixes, so they are separated:

    ``character_absent_from_table``
        Not in the dialect's ``hanzi.xlsx`` at all. Needs new dialect data.
    ``polyphone_reading_mismatch``
        In the table, but only for other readings. The table is not at fault.
    ``tone_variant_not_in_syllable_table``
        The table defines the character for exactly this reading and the syllable
        family exists, so only this one tone is missing.
    ``dialect_value_not_in_syllable_table``
        The table defines the character for this reading, but the dialect syllable
        it maps to is absent from the dialect's own ``syllable.xlsx``. An internal
        inconsistency in the released tables.
    """
    if character in unconditional:
        if pinyin in syllable_entries:
            return "unreachable_after_tone_change"
        if strip_tone(pinyin) in syllable_bases:
            return "tone_variant_not_in_syllable_table"
        return "dialect_value_not_in_syllable_table"
    readings = conditional.get(character)
    if readings is None:
        return "character_absent_from_table"
    if pinyin not in readings:
        return "polyphone_reading_mismatch"
    if pinyin in syllable_entries:
        return "unreachable_after_tone_change"
    if strip_tone(pinyin) in syllable_bases:
        return "tone_variant_not_in_syllable_table"
    return "dialect_value_not_in_syllable_table"


def score_chunks(
    row_id: str,
    ipa_payload: str,
    characters: list[str],
    pinyin_units: list[str],
    vocabulary: set[str],
    remap: dict[str, str],
    punctuation: set[str],
) -> list[dict]:
    """Attribute one line's outcome back to individual characters.

    ``pinyin2ipa.format_ipa_output`` puts a literal ``|`` between units *before*
    turning each unit's internal commas into spaces, so splitting the final IPA on
    ``|`` recovers exactly one chunk per input character - including punctuation,
    and including ``#...#`` connected-speech blocks, which stage 5 re-splits into
    one unit per syllable anyway. That is what makes a sentence corpus as
    measurable as a single-character one: the leak can be pinned on a character
    instead of only on a line.
    """
    chunks = [chunk.strip() for chunk in ipa_payload.split("|")]
    rows: list[dict] = []
    for index, chunk in enumerate(chunks):
        character = characters[index] if index < len(characters) else ""
        pinyin = pinyin_units[index] if index < len(pinyin_units) else ""
        kept_before, dropped_before = normalizer_out(chunk, vocabulary, {}, punctuation)
        kept_after, dropped_after = normalizer_out(chunk, vocabulary, remap, punctuation)
        rows.append(
            {
                "id": row_id,
                "index": index,
                "character": character,
                "pinyin": pinyin,
                "ipa": chunk,
                "kept_before": kept_before,
                "dropped_before": dropped_before,
                "kept_after": kept_after,
                "dropped_after": dropped_after,
            }
        )
    return rows


def sweep_dialect(
    dialect: str,
    corpus: Path,
    corpus_pinyin: list[str],
    work_dir: Path,
    frontend_root: Path,
    python_exe: str,
    vocabulary: set[str],
    remap: dict[str, str],
    punctuation: set[str],
    apply_repair: bool = False,
) -> dict:
    """Run the pipeline for one dialect and score every character."""
    outcome = run_pipeline(dialect, corpus, work_dir, frontend_root, python_exe)
    if not outcome["ok"]:
        return {"dialect": dialect, "error": f"stage {outcome['stage']} failed", "log": outcome["log"]}

    repair_report = None
    if apply_repair and dialect != "putonghua":
        dialect_syllables = load_syllable_entries(
            frontend_root / DIALECT_ROOT_NAME / dialect / "syllable.xlsx"
        )
        fallbacks, _ = find_fallbacks(work_dir / f"{dialect}_hanzi.txt", dialect_syllables)
        rows, repair_report = repair(
            work_dir / f"{dialect}_ipa_format.txt", fallbacks, Mandarins(frontend_root)
        )
        write_rows(work_dir / f"{dialect}_ipa_repaired.txt", rows)

    # Token classification reads the *last* stage: ``ipa_tone.py`` rewrites the
    # digit contours (``ˈi¹¹³``) into the letter marks the vocabulary contains
    # (``ˈiᴴᴴᴸ``); scoring the pre-tone file would call every toned syllable a
    # loss. The leak log is written by the pinyin-to-IPA stage, so it is read there.
    ipa_rows = read_stage_rows(
        work_dir / f"{dialect}_ipa_repaired.txt" if repair_report is not None else outcome["final"]
    )
    dialect_dir = frontend_root / DIALECT_ROOT_NAME / dialect
    hanzi_path = dialect_dir / "hanzi.xlsx"
    unconditional: set[str] = set()
    conditional: dict[str, set[str]] = {}
    if hanzi_path.is_file():
        unconditional, conditional = load_hanzi_map(hanzi_path)
    syllable_path = dialect_dir / ("base_syllable.xlsx" if dialect == "putonghua" else "syllable.xlsx")
    syllable_entries = load_syllable_entries(syllable_path) if syllable_path.is_file() else {}
    syllable_bases = {strip_tone(key) for key in syllable_entries}

    corpus_map = corpus_rows(corpus)
    leaks = read_leak_log((work_dir / f"{dialect}_ipa.txt").with_suffix(".txt.log"))
    leaked_items = [item for item in leaks if item not in PUNCTUATION]
    leaked_punctuation = [item for item in leaks if item in PUNCTUATION]

    records: list[dict] = []
    misaligned_lines = 0
    for row_id, raw_ipa in ipa_rows.items():
        text, pinyin = corpus_map.get(row_id, ("", ""))
        characters = list(text)
        units = pinyin.split() if pinyin else []
        if len(raw_ipa.split("|")) != len(characters) or len(units) != len(characters):
            # Only reachable for ``#...#`` text, which none of the corpora write.
            misaligned_lines += 1
            continue
        records.extend(score_chunks(row_id, raw_ipa, characters, units, vocabulary, remap, punctuation))

    silent = [row for row in records if not row["kept_after"]]
    partial = [row for row in records if row["kept_after"] and row["dropped_after"]]
    rescued = [row for row in records if not row["kept_before"] and row["kept_after"]]
    recovered = [row for row in records if row["dropped_before"] and not row["dropped_after"]]

    def is_pinyin_leak(row: dict) -> bool:
        return any(_PINYIN_LIKE.match(token) for token in row["dropped_after"])

    silent_by_leak = [row for row in silent if is_pinyin_leak(row)]
    silent_by_phone = [row for row in silent if not is_pinyin_leak(row)]

    for row in silent_by_leak:
        row["leak_cause"] = classify_leak(
            row["character"], row["pinyin"], unconditional, conditional,
            syllable_entries, syllable_bases,
        )
    causes = Counter(row["leak_cause"] for row in silent_by_leak)
    by_cause: dict[str, list[dict]] = defaultdict(list)
    for row in silent_by_leak:
        by_cause[row["leak_cause"]].append(row)

    silent_characters = {row["character"] for row in silent}

    return {
        "dialect": dialect,
        "corpus_lines": len(corpus_map),
        "lines_scored": len({row["id"] for row in records}) if records else 0,
        "misaligned_lines": misaligned_lines,
        "characters_scored": len(records),
        "distinct_characters": len({row["character"] for row in records}),
        "vendor_leaked_items": sorted(set(leaked_items)),
        "vendor_leaked_item_count": len(set(leaked_items)),
        "vendor_leaked_punctuation": sorted(set(leaked_punctuation)),
        "silent_lines": len(silent),
        "silent_line_share": round(len(silent) / len(records), 4) if records else 0.0,
        "silent_distinct_characters": len(silent_characters),
        "silent_by_raw_pinyin_leak": len(silent_by_leak),
        "silent_by_unknown_phone": len(silent_by_phone),
        "leak_causes": dict(causes),
        "leak_by_cause": {
            cause: [
                {"character": row["character"], "pinyin": row["pinyin"], "ipa": row["ipa"]}
                for row in rows[:60]
            ]
            for cause, rows in sorted(by_cause.items())
        },
        "leak_character_list": "".join(row["character"] for row in silent_by_leak)[:600],
        "phone_examples": [
            {"character": row["character"], "ipa": row["ipa"], "dropped": row["dropped_after"]}
            for row in silent_by_phone[:15]
        ],
        "partial_lines": len(partial),
        "partial_examples": [
            {"character": row["character"], "ipa": row["ipa"], "dropped": row["dropped_after"]}
            for row in partial[:15]
        ],
        "rescued_by_remap": len(rescued),
        "recovered_by_remap": len(recovered),
        "recovered_examples": [
            {
                "character": row["character"],
                "ipa": row["ipa"],
                "was": row["dropped_before"],
                "now": row["kept_after"],
            }
            for row in recovered[:15]
        ],
        "most_common_dropped_tokens": Counter(
            token for row in records for token in row["dropped_after"]
        ).most_common(20),
        "syllable_entries": len(syllable_entries),
        "syllable_bases": len(syllable_bases),
        "repair_applied": repair_report is not None,
        "repair": repair_report,
        "records": records,
    }


def corpus_rows(path: Path) -> dict[str, tuple[str, str]]:
    """Return ``id -> (character, mandarin_pinyin)`` for the corpus as written."""
    rows: dict[str, tuple[str, str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            rows[parts[0]] = (parts[1], parts[2])
    return rows


# ----------------------------------------------------------------------- main ---


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--frontend-root", default=str(DEFAULT_FRONTEND))
    parser.add_argument("--dialect", action="append", default=None)
    parser.add_argument("--limit", type=int, default=0, help="cap the lines per dialect (0 = all)")
    parser.add_argument(
        "--mode",
        choices=("chars", "sentences"),
        default="chars",
        help="chars: one character per line, best-case reading; "
             "sentences: Chinese prose, so the G2P picks readings in context",
    )
    parser.add_argument(
        "--sentence-source",
        action="append",
        default=None,
        help="file to mine for sentences (default: the repository's own docs)",
    )
    parser.add_argument("--sentence-min", type=int, default=8)
    parser.add_argument("--sentence-max", type=int, default=48)
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "output" / "frontend_loss_sweep"))
    parser.add_argument("--work-dir", default=None, help="defaults to <out-dir>/work")
    parser.add_argument("--json-out", default=None, help="defaults to <out-dir>/report.json")
    parser.add_argument("--build-corpus", action="store_true")
    parser.add_argument("--corpus-root", default=None, help="reuse a corpus directory")
    parser.add_argument(
        "--repair",
        action="store_true",
        help="apply the marked Mandarin fallback before scoring, so the report "
             "shows the loss that remains after the repair",
    )
    parser.add_argument("--python-exe", default=sys.executable)
    args = parser.parse_args()

    root = Path(args.root)
    frontend_root = Path(args.frontend_root)
    dialect_root = frontend_root / DIALECT_ROOT_NAME
    out_dir = Path(args.out_dir)
    corpus_root = Path(args.corpus_root) if args.corpus_root else out_dir / "corpus"
    work_dir = Path(args.work_dir) if args.work_dir else out_dir / "work"
    json_out = Path(args.json_out) if args.json_out else out_dir / "report.json"
    corpus_root.mkdir(parents=True, exist_ok=True)

    characters = consensus_characters(dialect_root)
    print(f"consensus characters: {len(characters)}")

    sentence_corpus: Path | None = None
    if args.mode == "sentences":
        sources = (
            [Path(item) for item in args.sentence_source]
            if args.sentence_source
            else sorted((REPO_ROOT / "docs").glob("*.md")) + [REPO_ROOT / "AGENTS.md"]
        )
        sentence_corpus = corpus_root / "sentences.txt"
        if args.build_corpus or not sentence_corpus.is_file():
            summary = build_sentence_corpus(
                sources, sentence_corpus, args.limit, args.sentence_min, args.sentence_max
            )
            print(
                f"sentence corpus: {summary['sentences']} sentences "
                f"(mean {summary['mean_characters']} characters, "
                f"{summary['skipped_misaligned']} dropped for misalignment)"
            )
        print(f"  sources: {', '.join(path.name for path in sources if path.is_file())}")

    # ``load_symbol_tables`` returns both sets bracket-wrapped; strip the brackets
    # so a token can be tested directly, as ``normalize_tokens`` sees it.
    ipa_set, punctuation_set = load_symbol_tables(root)
    vocabulary = {symbol[1:-1] for symbol in ipa_set}
    punctuation = {symbol[1:-1] for symbol in punctuation_set}
    remap = normalizer.load_remap(Path(normalizer.DEFAULT_REMAP))
    print(f"model vocabulary: {len(vocabulary)} symbols; remap: {len(remap)} mappings")

    dialects = args.dialect or sorted(
        directory.name
        for directory in dialect_root.iterdir()
        if directory.is_dir() and directory.name not in SKIPPED_DIALECTS
    )

    reports = []
    for dialect in dialects:
        print(f"\n===== {dialect} =====")
        dialect_dir = dialect_root / dialect
        hanzi_path = dialect_dir / "hanzi.xlsx"
        unconditional: set[str] = set()
        conditional: dict[str, set[str]] = {}
        if hanzi_path.is_file():
            unconditional, conditional = load_hanzi_map(hanzi_path)

        corpus = sentence_corpus if sentence_corpus is not None else corpus_root / f"{dialect}.txt"
        if sentence_corpus is not None:
            corpus_pinyin = []
        elif args.build_corpus or not corpus.is_file():
            summary, corpus_pinyin = write_dialect_corpus(
                characters, unconditional, conditional, corpus, limit=args.limit
            )
            print(
                f"  corpus: {summary['lines']} lines "
                f"({summary['from_table_reading']} chars keyed to a stated reading, "
                f"{summary['unconditional_reading']} mapped for any reading, "
                f"{summary['from_g2p_reading']} from pypinyin)"
            )
            if summary["skipped_no_mandarin_reading"] or summary["skipped_unalignable"]:
                print(
                    f"    skipped {summary['skipped_no_mandarin_reading']} with no Mandarin reading "
                    f"and {summary['skipped_unalignable']} that are not one syllable"
                )
        else:
            corpus_pinyin = [pinyin for _, _, pinyin in
                             (line.split("\t") for line in corpus.read_text(encoding="utf-8").splitlines() if line.strip())]

        report = sweep_dialect(
            dialect, corpus, corpus_pinyin, work_dir, frontend_root, args.python_exe,
            vocabulary, remap, punctuation, apply_repair=args.repair,
        )
        if "error" not in report:
            report.update(polyphone_exposure(characters, conditional))
        reports.append(report)

        if "error" in report:
            print(f"  {report['error']}")
            for entry in report.get("log", []):
                if entry["returncode"] != 0:
                    print(f"    {entry['script']}: rc={entry['returncode']}")
                    print(f"    {entry['stderr_tail']}")
            continue

        print(
            f"  scored lines          : {report['lines_scored']}"
            f" / {report['distinct_characters']} distinct characters"
        )
        print(
            f"  SILENT lines          : {report['silent_lines']}"
            f"  ({report['silent_line_share'] * 100:.1f}%,"
            f" {report['silent_distinct_characters']} distinct characters)"
            f"  [raw-pinyin leak {report['silent_by_raw_pinyin_leak']},"
            f" unknown phone {report['silent_by_unknown_phone']}]"
        )
        print(f"  partial lines         : {report['partial_lines']}")
        print(f"  rescued by remap      : {report['rescued_by_remap']}")
        print(f"  recovered by remap    : {report['recovered_by_remap']}")
        print(f"  vendor-reported leaks : {report['vendor_leaked_item_count']} distinct")
        for cause, count in sorted(report["leak_causes"].items()):
            print(f"    {cause:<38} {count}")
            samples = report["leak_by_cause"].get(cause, [])[:10]
            if samples:
                print("      " + "".join(row["character"] for row in samples)
                      + "   e.g. " + ", ".join(f"{r['character']}={r['pinyin']}" for r in samples[:3]))
        print(f"  polyphone exposure    : {report['polyphone_exposed_characters']} characters"
              " whose usual Mandarin reading the table lacks")
        if report.get("repair"):
            repair_stats = report["repair"]
            print(f"  Mandarin fallback     : {repair_stats['substituted']} syllables spoken in "
                  f"Mandarin instead of being dropped")
            if repair_stats["unmapped_by_mandarin_table"]:
                print(f"    still unmapped: {repair_stats['unmapped_by_mandarin_table'][:10]}")
        if report["phone_examples"]:
            print("  unknown-phone losses:")
            for row in report["phone_examples"][:8]:
                print(f"    {row['character']}  ->  {row['ipa']!r} dropped={row['dropped']}")

    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(
        json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nwrote {json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())