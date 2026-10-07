#!/usr/bin/env python3
"""Phase 1 dialect baseline: can DiaMoE pronounce each dialect at all?

For every dialect the IPA frontend can convert, this sweep:

1. converts a fixed probe set from Mandarin pinyin into dialect IPA and measures
   how far that IPA diverges from the Mandarin IPA of the same sentence;
2. reports how many IPA phones the acoustic model actually has embeddings for
   (a dropped phone is a phone that will never be spoken);
3. synthesises with the official DiaMoE dialect prompt as the reference voice;
4. records which MoE expert the target text routes to;
5. reads the result back with a Mandarin ASR and reports CER.

Timbre is deliberately out of scope: this run answers "is the dialect right",
not "is it the user's voice". A second pass repeats the probes with the user's
own Mandarin reference clip, which shows how much dialect survives when the
reference carries no dialect at all - that is the question the product depends
on, because users will only ever supply Mandarin.

The ASR number is a coarse gate, not a verdict: Shanghai and Min output cannot
transcribe cleanly into Mandarin characters even when the dialect is correct.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import soundfile as sf  # noqa: E402

from asr_readback import DEFAULT_ASR_DIR, cer, load_asr, transcribe  # noqa: E402
from diamoe_runtime import (  # noqa: E402
    DEFAULT_BASE_CKPT,
    DEFAULT_ROOT,
    build_model,
    ipa_to_model_text,
    load_symbol_tables,
    prepare_reference,
    read_ipa_row,
    squeeze_reference_silence,
    synthesize,
)

DEFAULT_PROBES = Path("third_party/DiaMoE-TTS/dialect_frontend/work/00_pinyin.txt")
DEFAULT_USER_REF_AUDIO = Path(
    "GPT-SoVITS/custom_data/sliced/\u8bad\u7ec3.wav_0000191040_0000343680.wav"
)
DEFAULT_USER_REF_IPA = Path(
    "third_party/DiaMoE-TTS/dialect_frontend/work/reference_ipa_format.txt"
)
DEFAULT_PROBE_IDS = "probe002,probe004"
CONTROL_DIALECT = "putonghua"

# Dialects that have both an IPA frontend dictionary and an official DiaMoE prompt.
OFFICIAL_PROMPT_DIALECTS = [
    ("chengdu", "chengdu_male_prompt"),
    ("shijiazhuang", "shijiazhuang_male_prompt"),
    ("zhengzhou", "zhengzhou_male_prompt"),
    ("nanjing", "nanjing_male_prompt"),
    ("xian", "xian_male_prompt"),
    ("shanghai", "shanghai_male_prompt"),
]

# Dialects the frontend can convert but that ship no official DiaMoE prompt.
FRONTEND_ONLY_DIALECTS = ["wuhan", "qingdao"]

# Dialects with an official prompt but no usable frontend dictionary need a
# different route (native GPT-SoVITS or a dialect-specific lexicon) and are
# reported as out of scope rather than silently skipped.
OUT_OF_SCOPE = [
    ("cantonese", "no IPA frontend lexicon; the native GPT-SoVITS <yue> path already covers it"),
    ("tianjin", "official prompt ships, but there is no IPA frontend lexicon"),
    ("hokkien", "gaoxiong frontend dir ships syllable/tone tables only (no hanzi/word tables)"),
]

_PINYIN_LIKE = re.compile(r"^[A-Za-z]+\d*$")

# Chunks consisting only of these are punctuation, not syllables.
PUNCTUATION_TOKENS = set("|\u3002\uff0c\uff01\uff1f\u3001\uff1b\uff1a")


def read_probes(path: Path) -> dict[str, dict[str, str]]:
    probes: dict[str, dict[str, str]] = {}
    for row in path.read_text(encoding="utf-8").splitlines():
        if not row.strip():
            continue
        parts = row.split("\t")
        if len(parts) < 3:
            continue
        probes[parts[0]] = {"text": parts[1], "pinyin": parts[2]}
    return probes


def read_ipa_rows(path: Path) -> dict[str, str]:
    rows: dict[str, str] = {}
    for row in path.read_text(encoding="utf-8").splitlines():
        if not row.strip():
            continue
        parts = row.split("\t")
        if len(parts) < 3:
            continue
        rows[parts[0]] = parts[2]
    return rows


def syllables(raw_ipa: str) -> list[str]:
    """Syllable chunks of a DiaMoE IPA line, punctuation excluded.

    Punctuation occupies its own ``|`` chunk, so counting every chunk would
    overstate the syllable count and quietly skew any per-syllable rate.
    """
    chunks = []
    for chunk in raw_ipa.split("|"):
        chunk = chunk.strip()
        if not chunk or all(token in PUNCTUATION_TOKENS for token in chunk.split()):
            continue
        chunks.append(chunk)
    return chunks


def divergence(reference_ipa: str, dialect_ipa: str) -> dict:
    """How far the dialect IPA moved away from the Mandarin IPA of the same line."""
    base = syllables(reference_ipa)
    target = syllables(dialect_ipa)
    aligned = min(len(base), len(target))
    changed = sum(1 for index in range(aligned) if base[index] != target[index])
    return {
        "mandarin_syllables": len(base),
        "dialect_syllables": len(target),
        "changed_syllables": changed + abs(len(base) - len(target)),
        "divergence": round((changed + abs(len(base) - len(target))) / max(len(base), len(target), 1), 4),
    }


# fix_erhua.py predates the dialect flag; every later stage needs it.
STAGES_WITHOUT_DIALECT = {"fix_erhua.py"}


def prepare_squeezed_reference(ref_audio, ref_text: str, label: str, out_dir: Path, squeeze: bool):
    """Prepare a reference clip and optionally drop its long internal pauses."""
    if not squeeze:
        return ref_audio, ref_text, None
    squeezed, rate = squeeze_reference_silence(ref_audio)
    raw_seconds = reference_seconds(ref_audio)
    new_seconds = len(squeezed) / rate
    if raw_seconds - new_seconds < 0.15:
        return ref_audio, ref_text, None

    ref_dir = out_dir / "reference"
    ref_dir.mkdir(parents=True, exist_ok=True)
    squeezed_path = ref_dir / f"{label}_squeezed.wav"
    sf.write(str(squeezed_path), squeezed, rate)
    audio, text = prepare_reference(squeezed_path, ref_text)
    return audio, text, {
        "raw_seconds": round(raw_seconds, 3),
        "squeezed_seconds": round(reference_seconds(audio), 3),
        "how": "internal pauses longer than 0.25s replaced by 0.06s",
    }


def reference_seconds(ref_audio) -> float:
    """Duration of a prepared reference clip, whether it came back as a path or a tensor."""
    if isinstance(ref_audio, (str, Path)):
        return sf.info(str(ref_audio)).duration
    array = ref_audio.detach().cpu().numpy() if hasattr(ref_audio, "detach") else ref_audio
    return float(len(array)) / 24000.0


def run_stage(script: str, input_path: Path, output_path: Path, frontend_root: Path, dialect: str, python_exe: str) -> Path:
    # The vendored scripts are launched with cwd=frontend_root (they resolve their
    # xlsx tables relative to it), so every path handed to them must be absolute.
    input_path = input_path.resolve()
    output_path = output_path.resolve()
    cmd = [python_exe, script, "-i", str(input_path), "-o", str(output_path)]
    if script not in STAGES_WITHOUT_DIALECT:
        cmd += ["-d", dialect]
    completed = subprocess.run(
        cmd, cwd=str(frontend_root), capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if completed.returncode != 0:
        raise SystemExit(
            f"frontend stage {script} failed for {dialect}:\n{completed.stdout}\n{completed.stderr}"
        )
    if not output_path.exists():
        raise SystemExit(f"frontend stage {script} produced no output for {dialect}")
    return output_path


def run_frontend(dialect: str, probes_path: Path, work_dir: Path, frontend_root: Path, python_exe: str) -> Path:
    """Run the vendored 6-step Mandarin-to-dialect IPA pipeline for one dialect."""
    work_dir.mkdir(parents=True, exist_ok=True)
    current = work_dir / f"{dialect}_00_pinyin.txt"
    current.write_text(probes_path.read_text(encoding="utf-8"), encoding="utf-8")

    if dialect != CONTROL_DIALECT:
        current = run_stage("fix_erhua.py", current, work_dir / f"{dialect}_01_fix.txt", frontend_root, dialect, python_exe)
        current = run_stage("hanzi2dialect_pinyin.py", current, work_dir / f"{dialect}_02_hanzi.txt", frontend_root, dialect, python_exe)
        current = run_stage("word2dialect_pinyin.py", current, work_dir / f"{dialect}_03_word.txt", frontend_root, dialect, python_exe)
    current = run_stage("liandu_tone.py", current, work_dir / f"{dialect}_04_liandu.txt", frontend_root, dialect, python_exe)
    current = run_stage("pinyin2ipa.py", current, work_dir / f"{dialect}_05_ipa.txt", frontend_root, dialect, python_exe)
    current = run_stage("ipa_tone.py", current, work_dir / f"{dialect}_06_ipa_format.txt", frontend_root, dialect, python_exe)
    return current


def pacing_speed(ref_seconds: float, ref_text: str, gen_text: str, syllable_count: int, rate: float, hop_length: int, sample_rate: int) -> float:
    """Pick the F5 ``speed`` that lands the line on a fixed seconds-per-syllable.

    F5 sizes the output from the reference clip: ``ref_audio_len / ref_text_len *
    gen_text_len``. That makes a short reference race through the sentence and a
    long one drag, which changes the dialect's intelligibility for reasons that
    have nothing to do with the dialect. Dividing by ``speed`` puts the pace back
    under our control, using only the vendor's supported knob.
    """
    ref_len = int(ref_seconds * sample_rate / hop_length)
    ref_tokens = len(ref_text.split(" "))
    default_gen_seconds = ref_len / ref_tokens * len(gen_text.split(" ")) * hop_length / sample_rate
    desired = syllable_count * rate
    if ref_tokens == 0 or desired <= 0:
        return 1.0
    return max(0.2, min(2.0, round(default_gen_seconds / desired, 4)))


def coverage_report(raw_ipa: str, ipa_set: set[str], punctuation: set[str]) -> dict:
    _, kept, dropped = ipa_to_model_text(raw_ipa, ipa_set, punctuation)
    unresolved = [token for token in dropped if _PINYIN_LIKE.match(token)]
    unknown = [token for token in dropped if not _PINYIN_LIKE.match(token)]
    total = len(kept) + len(dropped)
    return {
        "kept": len(kept),
        "dropped": len(dropped),
        "total": total,
        "dropped_unresolved_pinyin": unresolved,
        "dropped_unknown_phone": unknown,
        "coverage": round(len(kept) / total, 4) if total else 0.0,
        "full_coverage": not dropped,
    }


def evaluate(
    *,
    runtime: dict,
    asr,
    dialect: str,
    probe_ids: list[str],
    ipa_rows: dict[str, str],
    mandarin_rows: dict[str, str],
    probes: dict[str, dict[str, str]],
    ref_audio_raw,
    ref_text: str,
    ref_label: str,
    ref_dropped: list[str],
    out_dir: Path,
    ipa_set: set[str],
    punctuation: set[str],
    seed: int,
    candidates: int,
    ref_seconds: float,
    rate: float | None,
    hop_length: int,
    sample_rate: int,
    ref_squeeze: dict | None = None,
) -> dict:
    audio_dir = out_dir / "audio" / dialect
    audio_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for probe_id in probe_ids:
        raw_ipa = ipa_rows.get(probe_id)
        if raw_ipa is None:
            records.append({"probe": probe_id, "error": "missing frontend row"})
            continue
        reference_text = probes[probe_id]["text"]
        gen_text, _, _ = ipa_to_model_text(raw_ipa, ipa_set, punctuation)
        if not gen_text.strip():
            records.append({"probe": probe_id, "error": "frontend produced no model tokens"})
            continue

        syllable_count = len(syllables(raw_ipa))
        speed = 1.0
        if rate:
            speed = pacing_speed(ref_seconds, ref_text, gen_text, syllable_count, rate, hop_length, sample_rate)

        candidate_dir = audio_dir / "candidates"
        candidate_dir.mkdir(parents=True, exist_ok=True)
        attempts = []
        for index, candidate_seed in enumerate(CANDIDATE_SEEDS[:candidates]):
            audio, audio_rate, gate = synthesize(
                runtime, ref_audio_raw, ref_text, gen_text, seed=candidate_seed, speed=speed,
                capture_gate=index == 0, quiet=True,
            )
            candidate_path = candidate_dir / f"{probe_id}_{ref_label}_s{candidate_seed}.wav"
            sf.write(str(candidate_path), audio, audio_rate)
            readback = transcribe(asr, candidate_path)
            duration = len(audio) / audio_rate
            stats = waveform_stats(audio, audio_rate)
            attempts.append({
                "seed": candidate_seed,
                "readback": readback,
                "cer": round(cer(reference_text, readback), 4),
                "duration": round(duration, 3),
                "speed": speed,
                "syllables": syllable_count,
                "gate": gate,
                "wav": str(candidate_path),
                **stats,
            })

        # Content that was never spoken is the failure mode users actually hear,
        # so ASR agreement comes first and trailing silence breaks ties.
        best = min(attempts, key=lambda item: (round(item["cer"], 2), item["trailing_silence"], item["longest_gap"]))
        wav_path = audio_dir / f"{probe_id}_{ref_label}.wav"
        source = Path(best["wav"])
        wav_path.write_bytes(source.read_bytes())

        records.append({
            "probe": probe_id,
            "reference_text": reference_text,
            "dialect_ipa": raw_ipa,
            "coverage": coverage_report(raw_ipa, ipa_set, punctuation),
            "divergence": divergence(mandarin_rows.get(probe_id, ""), raw_ipa),
            "gate": best["gate"] or next((a["gate"] for a in attempts if a["gate"]), None),
            "asr_readback": best["readback"],
            "cer": best["cer"],
            "duration": best["duration"],
            "speed": speed,
            "syllables": syllable_count,
            "syllables_per_second": round(syllable_count / best["duration"], 3) if best["duration"] else None,
            "nuclei": best["nuclei"],
            "longest_gap": best["longest_gap"],
            "trailing_silence": best["trailing_silence"],
            "seed": best["seed"],
            "candidates": attempts,
            "wav": str(wav_path),
        })

    scored = [record for record in records if "cer" in record]
    return {
        "dialect": dialect,
        "reference": ref_label,
        "reference_dropped_tokens": ref_dropped,
        "reference_squeeze": ref_squeeze,
        "records": records,
        "mean_cer": round(sum(r["cer"] for r in scored) / len(scored), 4) if scored else None,
        "mean_coverage": round(sum(r["coverage"]["coverage"] for r in scored) / len(scored), 4) if scored else None,
        "mean_divergence": round(sum(r["divergence"]["divergence"] for r in scored) / len(scored), 4) if scored else None,
        "experts": sorted({r["gate"]["top1_expert"] for r in scored if r.get("gate")}),
        "expert_agreement": min((r["gate"]["top1_agreement"] for r in scored if r.get("gate")), default=None),
    }


def waveform_stats(audio, rate: int) -> dict:
    """Syllable nuclei, internal pauses and trailing silence, straight from the signal.

    Sampling variance in F5 shows up as content that never gets spoken: the mel
    budget is fixed, so a bad seed simply leaves silence where syllables should
    be. This is the cheapest honest way to see that without trusting ASR text.
    """
    import numpy as np
    from scipy.signal import find_peaks

    samples = np.asarray(audio, dtype=np.float64)
    frame, hop = 240, 80
    if len(samples) < frame * 4:
        return {"nuclei": 0, "longest_gap": 0.0, "gaps_over_200ms": 0, "trailing_silence": 1.0}
    frames = 1 + (len(samples) - frame) // hop
    rms = np.array([np.sqrt(np.mean(samples[i * hop:i * hop + frame] ** 2)) for i in range(frames)])
    rms = np.convolve(rms, np.ones(3) / 3.0, mode="same")
    floor = float(np.percentile(rms, 5))
    top = float(rms.max())
    span = max(top - floor, 1e-9)
    env_rate = rate / hop
    peaks, _ = find_peaks(
        rms,
        distance=int(0.085 * env_rate),
        prominence=0.12 * span,
        height=floor + 0.10 * span,
    )
    quiet = rms <= (floor + 0.05 * span)
    runs, start = [], None
    for index, flag in enumerate(quiet):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(quiet)))
    gaps = [(end - begin) / env_rate for begin, end in runs if (end - begin) / env_rate >= 0.12]
    tail = samples[int(len(samples) * 0.8):]
    trailing_silence = float(np.mean(np.abs(tail) < 2e-3)) if tail.size else 1.0
    return {
        "nuclei": int(len(peaks)),
        "longest_gap": round(max(gaps), 3) if gaps else 0.0,
        "gaps_over_200ms": sum(1 for gap in gaps if gap > 0.2),
        "trailing_silence": round(trailing_silence, 3),
    }


# F5 is a diffusion-style sampler: the same text at a different seed can drop a
# different amount of content. A fixed spread beats sequential seeds, because
# neighbouring seeds correlate.
CANDIDATE_SEEDS = [333, 1, 2, 4, 5, 7, 11, 13]

# Calibrated against a human listening round over 18 groups x 4 seeds: the
# automatic rule below agreed with the listener 2/18 times, i.e. below the 25%
# chance level, and in three groups the listener preferred the candidate it
# ranked worst. Mandarin-ASR agreement measures "sounds like Mandarin", which is
# not the same thing as "sounds like a good version of this dialect", so treat
# the chosen file as a convenience artefact, not as a judgement.
SELECTION_NOTE = (
    "candidate selection is NOT validated. The automatic rule is driven by Mandarin ASR "
    "agreement and scored 2/18 against both a naive and a blind human listening round "
    "(chance = 4.5/18). A blind re-test also showed that the apparent preference for seed "
    "333 was a filename artefact (10/18 when the seed was visible, 5/18 when it was not). "
    "Defaults to one render; raise --candidates only when a person is going to audition."
)


def record_syllable_rate(record: dict):
    """Syllables per second straight from the IPA and the rendered duration."""
    duration = record.get("duration")
    if not duration:
        return "-"
    return round(len(syllables(record.get("dialect_ipa", ""))) / duration, 2)


def render_markdown(results: dict) -> str:
    lines = ["# DiaMoE dialect baseline (Phase 1)", ""]
    lines.append(f"- probes: {', '.join(results['probe_ids'])}")
    for probe_id in results["probe_ids"]:
        lines.append(f"- {probe_id}: `{results['probe_texts'][probe_id]}`")
    lines.append(f"- nfe {results['nfe']} / cfg {results['cfg']}")
    lines.append(
        f"- reference mode: {results.get('reference_mode', 'both')} / "
        f"{results.get('candidates', 1)} candidate seeds per line, best one kept "
        f"({', '.join(str(s) for s in CANDIDATE_SEEDS[:results.get('candidates', 1)])})"
    )
    if results.get("rate"):
        lines.append(f"- pacing: target {results['rate']} s per syllable, F5 `speed` solved per probe")
    else:
        lines.append("- pacing: left to the reference clip (F5 default), so pace varies with reference length")
    lines.append(
        f"- base checkpoint `{results['base_ckpt']}` (missing={results['base_load']['missing_keys']}, "
        f"unexpected={results['base_load']['unexpected_keys']}), device {results['device']}"
    )
    if results.get("selection_note"):
        lines.append(f"- selection: {results['selection_note']}")
    lines.append("")
    lines.append("`divergence` is how far the dialect IPA moved from the Mandarin IPA of the same sentence:")
    lines.append("0.0 would mean the frontend did nothing and the output is just Mandarin.")
    lines.append("`coverage` is the share of frontend IPA phones the acoustic model has embeddings for.")
    lines.append("")

    for group_key, title in (
        ("official_prompt", "A. Official dialect prompt as the reference voice"),
        ("user_reference", "B. User Mandarin reference (no dialect in the reference)"),
        ("frontend_only", "C. Frontend-only dialects and the Mandarin control"),
    ):
        entries = results["groups"].get(group_key)
        if not entries:
            continue
        lines.append(f"## {title}")
        lines.append("")
        lines.append("| dialect | divergence | coverage | dropped tokens | MoE expert | expert prob | mean CER |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for entry in entries:
            dropped = sorted({
                token
                for record in entry["records"] if "coverage" in record
                for token in record["coverage"]["dropped_unresolved_pinyin"] + record["coverage"]["dropped_unknown_phone"]
            })
            probs = [r["gate"]["top1_prob"] for r in entry["records"] if r.get("gate")]
            lines.append(
                f"| {entry['dialect']} | {entry['mean_divergence']} | {entry['mean_coverage']} | "
                f"{', '.join(dropped) if dropped else '-'} | "
                f"{','.join(str(v) for v in entry['experts']) or '-'} | "
                f"{(max(probs) if probs else 0):.3f} | {entry['mean_cer']} |"
            )
        lines.append("")

        for entry in entries:
            lines.append(f"### {entry['dialect']} ({entry['reference']})")
            lines.append("")
            squeeze = entry.get("reference_squeeze")
            if squeeze:
                lines.append(
                    f"reference clip de-paused: {squeeze['raw_seconds']}s -> {squeeze['squeezed_seconds']}s "
                    f"({squeeze['how']})"
                )
                lines.append("")
            lines.append("| probe | reference text | ASR readback | CER | dur | syl/s | nuclei/target | max gap | seed | expert | wav |")
            lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
            for record in entry["records"]:
                if "cer" not in record:
                    lines.append(f"| {record['probe']} | - | {record.get('error', 'error')} | - | - | - | - | - | - | - | - |")
                    continue
                expert = record["gate"]["top1_expert"] if record.get("gate") else "-"
                wav = Path(record["wav"]).name
                target = len(syllables(record.get("dialect_ipa", "")))
                lines.append(
                    f"| {record['probe']} | {record['reference_text']} | {record['asr_readback']} | "
                    f"{record['cer']} | {record['duration']}s | {record_syllable_rate(record)} | "
                    f"{record.get('nuclei', '-')}/{target} | {record.get('longest_gap', '-')}s | "
                    f"{record.get('seed', '-')} | {expert} | {wav} |"
                )
            lines.append("")

    lines.append("## Out of scope in this run")
    lines.append("")
    for dialect, reason in results["out_of_scope"]:
        lines.append(f"- **{dialect}**: {reason}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diamoe-root", default=str(DEFAULT_ROOT))
    parser.add_argument("--base-ckpt", default=DEFAULT_BASE_CKPT)
    parser.add_argument("--probes", default=str(DEFAULT_PROBES))
    parser.add_argument("--probe-ids", default=DEFAULT_PROBE_IDS)
    parser.add_argument("--user-ref-audio", default=str(DEFAULT_USER_REF_AUDIO))
    parser.add_argument("--user-ref-ipa-file", default=str(DEFAULT_USER_REF_IPA))
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--asr-model", default=str(DEFAULT_ASR_DIR))
    parser.add_argument("--seed", type=int, default=333)
    parser.add_argument("--nfe", type=int, default=32)
    parser.add_argument("--cfg", type=float, default=2.0)
    parser.add_argument("--reference", choices=["user", "official", "both"], default="user",
                        help="which reference voice to synthesise with. 'user' (default) uses the "
                             "user's own clip: dialect identity comes from the IPA frontend, and the "
                             "official DiaMoE prompts talk roughly twice as fast, which is what makes "
                             "the generated syllables collapse.")
    parser.add_argument("--reference-squeeze", dest="reference_squeeze", action="store_true", default=True,
                        help="replace long internal pauses in the reference clip (default on). F5 copies "
                             "the reference's silences into the output, and the fixed mel budget makes "
                             "the syllables around them collapse.")
    parser.add_argument("--no-reference-squeeze", dest="reference_squeeze", action="store_false")
    parser.add_argument("--candidates", type=int, default=1,
                        help="render this many seeds per line and keep one. Default is 1: candidate "
                             "selection has not been shown to beat chance, so extra renders buy "
                             "nothing unless a person will audition them (see "
                             "scripts/dialect_blind_audition.py).")
    parser.add_argument("--skip-user-reference", action="store_true")
    parser.add_argument("--rate", type=float, default=0.0,
                        help="experimental: target seconds per syllable, solved through the F5 speed knob. "
                             "0 (default) leaves pacing to F5, which is the better-behaved setting: forcing a "
                             "slower pace makes the model fall back to repeating the reference text.")
    args = parser.parse_args()

    root = Path(args.diamoe_root).resolve()
    frontend_root = root / "dialect_frontend"
    prompts_dir = root / "prompts"
    probes_path = Path(args.probes)
    probes = read_probes(probes_path)
    probe_ids = [item.strip() for item in args.probe_ids.split(",") if item.strip()]
    missing = [item for item in probe_ids if item not in probes]
    if missing:
        raise SystemExit(f"unknown probe ids: {missing}")

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else Path("output/dialect_baseline") / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    frontend_dir = out_dir / "frontend"

    ipa_set, punctuation = load_symbol_tables(root)

    print("[1/4] running IPA frontend")
    frontend_outputs: dict[str, Path] = {}
    targets = [name for name, _ in OFFICIAL_PROMPT_DIALECTS] + FRONTEND_ONLY_DIALECTS + [CONTROL_DIALECT]
    for dialect in targets:
        try:
            frontend_outputs[dialect] = run_frontend(dialect, probes_path, frontend_dir, frontend_root, sys.executable)
            print(f"      {dialect}: ok")
        except SystemExit as exc:
            print(f"      {dialect}: frontend failed ({exc})")

    mandarin_rows = read_ipa_rows(frontend_outputs[CONTROL_DIALECT])

    print("[2/4] loading DiaMoE base")
    runtime = build_model(root=root, base_ckpt=args.base_ckpt)
    print(f"      base load: {runtime['base_load']}, device={runtime['device']}")

    print("[3/4] loading ASR readback model")
    asr = load_asr(args.asr_model)

    print("[4/4] synthesising")
    results = {
        "generated_at": stamp,
        "probe_ids": probe_ids,
        "probe_texts": {key: value["text"] for key, value in probes.items()},
        "seed": args.seed,
        "nfe": args.nfe,
        "cfg": args.cfg,
        "base_ckpt": runtime["base_ckpt"],
        "base_load": runtime["base_load"],
        "device": runtime["device"],
        "rate": args.rate or None,
        "candidates": args.candidates,
        "reference_mode": args.reference,
        "reference_squeeze": args.reference_squeeze,
        "selection_note": SELECTION_NOTE,
        "groups": {"official_prompt": [], "user_reference": [], "frontend_only": []},
        "out_of_scope": OUT_OF_SCOPE,
    }

    user_ref_text = None
    user_ref_dropped: list[str] = []
    if not args.skip_user_reference:
        _, _, user_raw = read_ipa_row(args.user_ref_ipa_file, 0)
        user_ref_text, _, user_ref_dropped = ipa_to_model_text(user_raw, ipa_set, punctuation)

    if args.reference in ("official", "both"):
        for dialect, prompt in OFFICIAL_PROMPT_DIALECTS:
            prompt_ipa_rows = read_ipa_rows(prompts_dir / f"{prompt}.txt")
            prompt_raw = next(iter(prompt_ipa_rows.values()))
            prompt_text, _, prompt_dropped = ipa_to_model_text(prompt_raw, ipa_set, punctuation)
            ref_audio, ref_text = prepare_reference(prompts_dir / f"{prompt}.wav", prompt_text)
            ref_audio, ref_text, squeeze_info = prepare_squeezed_reference(
                ref_audio, ref_text, f"official_{prompt}", out_dir, args.reference_squeeze)
            ref_seconds = reference_seconds(ref_audio)
            print(f"      {dialect}: official prompt reference")
            results["groups"]["official_prompt"].append(evaluate(
                runtime=runtime, asr=asr, dialect=dialect, probe_ids=probe_ids,
                ipa_rows=read_ipa_rows(frontend_outputs[dialect]), mandarin_rows=mandarin_rows,
                probes=probes, ref_audio_raw=ref_audio, ref_text=ref_text,
                ref_label="official_prompt", ref_dropped=prompt_dropped, out_dir=out_dir,
                ipa_set=ipa_set, punctuation=punctuation, seed=args.seed, candidates=args.candidates,
                ref_seconds=ref_seconds, rate=args.rate or None,
                hop_length=runtime["cfg"].model.mel_spec.hop_length,
                sample_rate=runtime["cfg"].model.mel_spec.target_sample_rate,
                ref_squeeze=squeeze_info,
            ))

    if user_ref_text is not None and args.reference in ("user", "both"):
        user_audio, user_text = prepare_reference(args.user_ref_audio, user_ref_text)
        user_audio, user_text, user_squeeze_info = prepare_squeezed_reference(
            user_audio, user_text, "user_reference", out_dir, args.reference_squeeze)
        user_ref_seconds = reference_seconds(user_audio)
        user_targets = [name for name, _ in OFFICIAL_PROMPT_DIALECTS] + FRONTEND_ONLY_DIALECTS + [CONTROL_DIALECT]
        for dialect in user_targets:
            if dialect not in frontend_outputs:
                continue
            group = "user_reference" if any(dialect == name for name, _ in OFFICIAL_PROMPT_DIALECTS) else "frontend_only"
            print(f"      {dialect}: user Mandarin reference")
            results["groups"][group].append(evaluate(
                runtime=runtime, asr=asr, dialect=dialect, probe_ids=probe_ids,
                ipa_rows=read_ipa_rows(frontend_outputs[dialect]), mandarin_rows=mandarin_rows,
                probes=probes, ref_audio_raw=user_audio, ref_text=user_text,
                ref_label="user_mandarin", ref_dropped=user_ref_dropped, out_dir=out_dir,
                ipa_set=ipa_set, punctuation=punctuation, seed=args.seed, candidates=args.candidates,
                ref_seconds=user_ref_seconds, rate=args.rate or None,
                hop_length=runtime["cfg"].model.mel_spec.hop_length,
                sample_rate=runtime["cfg"].model.mel_spec.target_sample_rate,
                ref_squeeze=user_squeeze_info,
            ))

    json_path = out_dir / "baseline.json"
    json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path = out_dir / "BASELINE.md"
    markdown_path.write_text(render_markdown(results), encoding="utf-8")
    print(json.dumps({"out_dir": str(out_dir), "json": str(json_path), "markdown": str(markdown_path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())