#!/usr/bin/env python3
"""Build a DiaMoE LoRA training dataset from the user's own Mandarin slices.

The pipeline mirrors the official DiaMoE ``putonghua`` frontend:

    text -> pinyin -> word mapping -> tone sandhi -> IPA -> IPA tone symbols

and then emits an Arrow dataset with the fields DiaMoE's ``CustomDataset``
expects: ``audio_path``, ``ori_text``, ``text``, ``duration``, ``dialect``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import types
from pathlib import Path

CANTONESE_CHARS = set("嘅唔咩冇咁哋喺咗佢睇啲嚟㗎嘢")
GUANGPU_MARKERS = (
    "最紧要", "呢个", "边度", "搞掂", "钟意", "点解",
    "呢排", "呢度", "几靓", "叻", "啩", "啫",
)
GUANGPU_COPULA = re.compile(r"我系|唔系|系唔系|就系|系咁|但系|真系|系啊|系咩")


def classify(text: str) -> str:
    """Return ``guangpu`` when the line carries Cantonese/Guangpu features."""
    if any(char in CANTONESE_CHARS for char in text):
        return "guangpu"
    if GUANGPU_COPULA.search(text):
        return "guangpu"
    if any(marker in text for marker in GUANGPU_MARKERS):
        return "guangpu"
    return "standard"


def read_slice_list(path: Path, sliced_dir: Path) -> list[dict[str, str]]:
    """Parse a GPT-SoVITS style ``path|speaker|lang|text`` list."""
    records: list[dict[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 4:
            continue
        audio_name = Path(parts[0]).name
        text = parts[3].strip()
        if not text:
            continue
        audio_path = sliced_dir / audio_name
        records.append(
            {
                "id": Path(audio_name).stem,
                "audio_name": audio_name,
                "audio_path": str(audio_path),
                "text": text,
                "subset": classify(text),
            }
        )
    return records


def _import_putonghua_frontend(frontend_dir: Path):
    """Import DiaMoE's ``Frontend`` while dodging its eager ``tools`` package."""
    if str(frontend_dir) not in sys.path:
        sys.path.insert(0, str(frontend_dir))

    # ``frontend.zh_frontend`` imports ``tools.log``, and ``tools/__init__.py``
    # re-imports ``frontend.dialect_frontend``, which imports zh_frontend again.
    # Pre-registering a bare path-only package breaks that cycle.
    stub = types.ModuleType("tools")
    stub.__path__ = [str(frontend_dir / "tools")]
    sys.modules.setdefault("tools", stub)

    from frontend.generate_lexicon import generate_lexicon
    from frontend.polyphonic import Polyphonic
    from frontend.symbols import zh_phonemes
    from frontend.zh_frontend import Frontend
    from g2pM import G2pM

    frontend = Frontend(g2p_model="pypinyin", vocab_phones=zh_phonemes)
    frontend.g2pM_model = G2pM()
    frontend.corrector = Polyphonic()
    frontend.pinyin2phone = generate_lexicon(with_tone=True, with_erhua=True)
    return frontend


def write_pinyin_file(records: list[dict[str, str]], frontend, out_path: Path) -> None:
    lines: list[str] = []
    for record in records:
        phones, _word2ph, _tones, pinyins, _oop, _zhongwens = (
            frontend.get_splited_phonemes_tones([record["text"]])
        )
        lines.append(f"{record['id']}\t{record['text']}\t{pinyins}")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_step(command: list[str], cwd: Path) -> None:
    result = subprocess.run(
        command, cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            "step failed (%s): %s"
            % (Path(command[1]).name, result.stderr[-800:])
        )


def run_putonghua_ipa(frontend_dir: Path, work_dir: Path) -> Path:
    """Run tone sandhi -> IPA -> IPA tone for ``putonghua``.

    ``word2dialect_pinyin`` maps Mandarin pinyin onto itself for ``putonghua``
    and hard-asserts on lines whose Cantonese characters have no Mandarin
    reading, so it is skipped here. ``liandu_tone`` already drops lines whose
    pinyin does not align with the text.
    """
    steps = [
        ("liandu_tone.py", work_dir / "01_pinyin.txt", work_dir / "03_tone.txt"),
        ("pinyin2ipa.py", work_dir / "03_tone.txt", work_dir / "04_ipa1.txt"),
        ("ipa_tone.py", work_dir / "04_ipa1.txt", work_dir / "05_ipa_format.txt"),
    ]
    for script_name, source, target in steps:
        run_step(
            [
                sys.executable, script_name,
                "--input", str(source),
                "--output", str(target),
                "--dialect", "putonghua",
            ],
            frontend_dir,
        )
    return steps[-1][2]


def load_prepare_ipa(diamoe_root: Path):
    """Load DiaMoE's ``prepare_ipa`` helpers without importing the package."""
    module_path = (
        diamoe_root
        / "diamoe_tts/src/f5_tts/train/datasets/prepare_ipa.py"
    )
    spec = importlib.util.spec_from_file_location("diamoe_prepare_ipa", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_records(
    records: list[dict[str, str]],
    ipa_file: Path,
    ipalist: set[str],
    punctuation: set[str],
) -> tuple[list[dict], list[str]]:
    """Attach IPA text, duration and dialect label to each slice."""
    import soundfile as sf

    ipa_by_id: dict[str, str] = {}
    for raw in ipa_file.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        parts = raw.split("\t")
        if len(parts) < 3:
            continue
        ipa_by_id[parts[0]] = parts[2]

    built: list[dict] = []
    skipped: list[str] = []
    for record in records:
        raw_ipa = ipa_by_id.get(record["id"], "")
        tokens: list[str] = []
        for token in raw_ipa.split():
            symbol = f"[{token}]"
            if symbol in ipalist:
                tokens.append(symbol)
            elif symbol in punctuation:
                tokens.append(token)
            elif symbol == "[|]":
                continue
            else:
                skipped.append(f"{record['id']}:{symbol}")
        if not tokens:
            skipped.append(f"{record['id']}:empty")
            continue
        try:
            duration = float(sf.info(record["audio_path"]).duration)
        except Exception as exc:  # noqa: BLE001 - surface the reason upstream
            skipped.append(f"{record['id']}:audio({exc})")
            continue
        built.append(
            {
                "audio_path": record["audio_path"],
                "ori_text": record["text"],
                "text": " ".join(tokens),
                "duration": duration,
                "dialect": "mandarin_user",
            }
        )
    return built, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diamoe-root", default="third_party/DiaMoE-TTS")
    parser.add_argument("--list", required=True, help="GPT-SoVITS style slice list")
    parser.add_argument("--sliced-dir", required=True, help="directory holding the wav slices")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--exp-name", required=True)
    parser.add_argument("--subset", choices=["all", "standard"], default="all")
    args = parser.parse_args()

    diamoe_root = Path(args.diamoe_root).resolve()
    frontend_dir = diamoe_root / "dialect_frontend"
    work_dir = Path(args.out_dir).resolve() / args.exp_name
    work_dir.mkdir(parents=True, exist_ok=True)

    records = read_slice_list(Path(args.list).resolve(), Path(args.sliced_dir).resolve())
    if args.subset == "standard":
        records = [record for record in records if record["subset"] == "standard"]
    if not records:
        print("no slices selected; nothing to do")
        return 1

    standard = sum(1 for record in records if record["subset"] == "standard")
    print(
        "selected %d slices (%d standard / %d guangpu)"
        % (len(records), standard, len(records) - standard)
    )

    (work_dir / "00_text.txt").write_text(
        "\n".join(f"{r['id']}\t{r['text']}" for r in records) + "\n",
        encoding="utf-8",
    )

    frontend = _import_putonghua_frontend(frontend_dir)
    write_pinyin_file(records, frontend, work_dir / "01_pinyin.txt")
    print(f"pinyin written: {work_dir / '01_pinyin.txt'}")

    ipa_file = run_putonghua_ipa(frontend_dir, work_dir)
    print(f"IPA written: {ipa_file}")

    prepare_ipa = load_prepare_ipa(diamoe_root)
    vocab_path = diamoe_root / "diamoe_tts/data/vocab.txt"
    punct_path = diamoe_root / "diamoe_tts/data/punctuation.txt"
    ipalist = set(prepare_ipa.load_ipa_list(str(vocab_path)))
    punctuation = set(prepare_ipa.load_punctuation_list(str(punct_path)))

    built, skipped = build_records(records, ipa_file, ipalist, punctuation)
    if not built:
        print("no usable rows produced")
        return 1

    durations = [row["duration"] for row in built]
    prepare_ipa.save_dataset(built, durations, str(work_dir), "dataset")
    manifest = {
        "subset": args.subset,
        "rows": len(built),
        "skipped": skipped,
        "total_seconds": round(sum(durations), 2),
        "standard_rows": sum(1 for r in records if r["subset"] == "standard"),
        "source_list": str(Path(args.list).resolve()),
        "arrow": str(work_dir / "dataset.arrow"),
    }
    (work_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())