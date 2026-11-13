"""FunASR readback used as an objective gate for generated dialect audio.

A Mandarin ASR failing on a dialect clip is not proof that the dialect is wrong:
Shanghai or Min output will never transcribe cleanly into Mandarin characters.
What the readback does catch reliably is gross failure - dropped syllables,
collapsed words, or a dialect so far off that the sentence is unrecognisable.
Treat it as a coarse gate, and pair it with the IPA coverage and MoE routing
numbers before drawing a conclusion.
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_ASR_DIR = (
    Path("GPT-SoVITS/tools/asr/models")
    / "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
)

# Halfwidth punctuation, CJK punctuation, and whitespace.
_PUNCTUATION = set(
    " \t\n"
    ",\\.!?;:\"'()\\[\\]{}<>~-_/|"
    "\uff0c\u3002\uff01\uff1f\u3001\uff1b\uff1a"
    "\u201c\u201d\u2018\u2019\uff08\uff09\u300a\u300b\u3008\u3009"
    "\u2014\u2026\u00b7\uff0f\uff5c\u3000"
)


def _require_funasr():
    try:
        import funasr  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "funasr is not installed in this interpreter; install it before running the readback gate"
        ) from exc


def load_asr(model_dir: str | Path = DEFAULT_ASR_DIR, device: str | None = None):
    """Load the local Paraformer checkpoint once and reuse it across clips."""
    _require_funasr()
    import torch
    from funasr import AutoModel

    model_path = Path(model_dir)
    if not model_path.exists():
        raise SystemExit(f"ASR model directory not found: {model_path}")
    resolved_device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    return AutoModel(model=str(model_path), disable_update=True, device=resolved_device)


def normalize(text: str) -> str:
    """Strip punctuation and whitespace so CER compares characters only."""
    return "".join(ch for ch in text if ch not in _PUNCTUATION and not ch.isspace())


def transcribe(model, wav_path: str | Path) -> str:
    result = model.generate(input=str(wav_path), batch_size_s=300)
    if not result:
        return ""
    return result[0].get("text", "")


def cer(reference: str, hypothesis: str) -> float:
    """Character error rate of hypothesis against reference, on normalized text."""
    import Levenshtein

    ref = normalize(reference)
    hyp = normalize(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    return Levenshtein.distance(ref, hyp) / len(ref)