"""Regression gate for the Guangpu G2P patch table in GPT-SoVITS's zh frontend.

The bug these tests guard against: ``GPT_SoVITS/text/chinese2.py`` carries a
Guangpu patch table (``GP_PINYIN`` / ``GP_PINYIN_FULL``) that forces Cantonese
characters to Guangpu readings (系->hei1, 嘅->ge3, ...). Two things can silently
break it:

1. A training-corpus character that the patch does not cover (the audio says
   Cantonese, the frontend reads Mandarin) - this was 唔/靓 before 2026-10-11.
2. A patch reading whose phonemes are missing from ``opencpop-strict.txt`` /
   ``symbols2.py`` - the frontend then either asserts or emits UNK.

Neither failure raises anywhere in the synthesis path, so these tests fail
loudly instead. They read the vendor files statically - no model load.

Run from the repository root:

    .venv\\Scripts\\python.exe -m unittest backend.tests.test_guangpu_g2p_patch -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOVITS_TEXT = REPO_ROOT / "GPT-SoVITS" / "GPT_SoVITS" / "text"
SLICE_LIST = REPO_ROOT / "GPT-SoVITS" / "custom_data" / "asr" / "sliced_corrected.list"

# Characters that are deliberately exempt from the patch, with the reason.
# 啱: Cantonese reading ngaam1 needs the ng initial, which opencpop-strict.txt
#     does not contain; the frontend falls back to the Mandarin yEn2. Appears
#     once in the corpus. Revisit if a symbol is ever added.
EXEMPT = {
    "啱": "粤音 ngaam1 需要 ng 声母，符号表无；普通话近似 yEn2（语料仅 1 次）",
}


def parse_patch_table(source: str) -> dict[str, str]:
    """Extract GP_PINYIN_FULL {char: pinyin} from the vendored chinese2.py."""
    block = re.search(r"GP_PINYIN_FULL\s*=\s*\{.*?\}", source, re.S)
    if not block:
        return {}
    return {
        key: value
        for key, value in re.findall(r'"([^"]+)":\s*"([^"]+)"', block.group(0))
    }


def parse_gp_pinyin(source: str) -> dict[str, tuple[str, str]]:
    """Extract GP_PINYIN {char: (initial, final)} for the non-g2pw branch."""
    block = re.search(r"GP_PINYIN\s*=\s*\{.*?\}", source, re.S)
    if not block:
        return {}
    return {
        key: (initial, final)
        for key, initial, final in re.findall(
            r'"([^"]+)":\s*\("([^"]+)",\s*"([^"]+)"\)', block.group(0)
        )
    }


def load_symbol_map() -> dict[str, str]:
    """opencpop-strict.txt: pinyin -> 'consonant final' (same source as chinese2)."""
    lines = (SOVITS_TEXT / "opencpop-strict.txt").read_text(encoding="utf-8").splitlines()
    return {
        line.split("\t")[0]: line.strip().split("\t")[1]
        for line in lines
        if "\t" in line
    }


def corpus_cantonese_chars() -> dict[str, int]:
    """Count Cantonese-character occurrences in the training slice texts."""
    if not SLICE_LIST.is_file():
        return {}
    counts: dict[str, int] = {}
    for line in SLICE_LIST.read_text(encoding="utf-8").splitlines():
        parts = line.split("|")
        if len(parts) < 4:
            continue
        for char in parts[3]:
            if re.match(r"[\u3400-\u4dbf\u4e00-\u9fa5]", char) and char in PATCH_CANDIDATES:
                counts[char] = counts.get(char, 0) + 1
    return counts


# The set of characters the Guangpu corpus is allowed to use.
PATCH_CANDIDATES = set("嘅唔咩冇咁哋喺咗佢睇啲嚟㗎嘢系靓畀乜啱")


@unittest.skipUnless(SOVITS_TEXT.joinpath("chinese2.py").is_file(), "vendored GPT-SoVITS text tree missing")
class GuangpuG2pPatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (SOVITS_TEXT / "chinese2.py").read_text(encoding="utf-8")
        cls.full = parse_patch_table(cls.source)
        cls.gp = parse_gp_pinyin(cls.source)
        cls.symbol_map = load_symbol_map()

    def test_full_patch_matches_gp_patch_keys(self) -> None:
        """GP_PINYIN (pypinyin branch) and GP_PINYIN_FULL (g2pw branch) must cover
        the same characters. Their spelling of the same reading differs by design
        (e.g. 嘢: (y,E5) vs ye5 - the final is written as a symbol in one and as
        pinyin in the other), so only the key sets must agree exactly."""
        self.assertTrue(self.full, "GP_PINYIN_FULL is empty")
        self.assertTrue(self.gp, "GP_PINYIN is empty")
        self.assertCountEqual(set(self.full), set(self.gp))

    def test_every_existing_patch_reading_has_symbols(self) -> None:
        """Each patched reading must map to real phonemes, both in opencpop and in
        the v2 symbol table; otherwise the frontend would assert or emit UNK."""
        import sys as _sys

        symbols = None
        # symbols2.py is importable without touching torch/model files.
        text_dir = str(SOVITS_TEXT)
        if text_dir not in _sys.path:
            _sys.path.insert(0, text_dir)
        try:
            import symbols2

            symbols = set(symbols2.symbols)
        except Exception as exc:  # pragma: no cover - environment breakage
            self.fail(f"cannot import symbols2: {exc}")

        for char, pinyin in self.full.items():
            base, tone = pinyin[:-1], pinyin[-1]
            self.assertIn(base, self.symbol_map, f"{char}: no opencpop entry for {base}")
            consonant, final = self.symbol_map[base].split(" ")
            self.assertIn(consonant, symbols, f"{char}: consonant {consonant} not in symbols2")
            self.assertIn(final + tone, symbols, f"{char}: final {final}{tone} not in symbols2")

    def test_every_corpus_cantonese_char_is_patched_or_exempt(self) -> None:
        """Training text characters with Cantonese readings must not be read in
        Mandarin by the frontend. 啱 is the one justified exemption."""
        if not corpus_cantonese_chars():
            self.skipTest(f"slice list missing: {SLICE_LIST}")
        missing = {
            char: count
            for char, count in corpus_cantonese_chars().items()
            if char not in self.full and char not in EXEMPT
        }
        self.assertEqual(
            missing, {},
            "未入广普补丁的语料粤字（训练文本音素与粤腔音频错位）: "
            + ", ".join(f"{k}×{v}" for k, v in sorted(missing.items())),
        )

    def test_no_legacy_mandarin_readings_for_patched_chars(self) -> None:
        """The patched readings must not be the Mandarin readings that used to leak
        in (系=xì, 唔=wú, 靓=jing4...). This pins the audit's intent."""
        forbidden = {"系": {"xi"}, "唔": {"wu"}, "靓": {"jing", "liang"}}
        for char, bad in forbidden.items():
            if char in self.full:
                pinyin = self.full[char]
                self.assertNotIn(pinyin[:-1], bad, f"{char}: still reading Mandarin {pinyin}")


if __name__ == "__main__":
    unittest.main()