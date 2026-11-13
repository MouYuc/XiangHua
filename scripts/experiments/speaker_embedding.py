#!/usr/bin/env python3
"""Independent speaker embeddings, for measuring timbre without circular evidence.

Why this exists as its own module
---------------------------------

The kNN-VC conversion in this project is built from **WavLM-Large layer-6 features**.
Asking whether a converted clip sounds like the target voice by comparing those same
WavLM features would be circular: the output is a nearest-neighbour lookup in that
space, so similarity there is true by construction and says nothing about timbre.

This module therefore loads a completely different model -
``ERes2NetV2`` (a WeSpeaker / 3D-Speaker-family speaker-verification network) with
Kaldi filterbank features - which shares no weights, no features and no training
objective with the vocoder path it is used to judge. Speaker embedding cosine
similarity (SECS) from an independent model is the standard way to report voice
similarity in the TTS and voice-conversion literature; the point of the "independent"
qualifier is exactly this.

GPT-SoVITS ships the checkpoint and the architecture, so nothing is downloaded:
``GPT-SoVITS/GPT_SoVITS/pretrained_models/sv/pretrained_eres2netv2w24s4ep4.ckpt``.

The vendored ``GPT_SoVITS/sv.py`` cannot be imported as-is: it resolves both the
architecture directory and the checkpoint through ``os.getcwd()``, so it only works
when the process happens to be running from the GPT-SoVITS folder. The loading and
feature code is reproduced here with explicit paths instead, so the result does not
depend on the working directory.

Usage:
    .venv\\Scripts\\python.exe scripts\\experiments\\speaker_embedding.py a.wav b.wav
"""

from __future__ import annotations

import argparse
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]

GPT_SOVITS_ROOT = REPO_ROOT / "GPT-SoVITS"
ERES2NET_DIR = GPT_SOVITS_ROOT / "GPT_SoVITS" / "eres2net"
DEFAULT_CHECKPOINT = (
    GPT_SOVITS_ROOT / "GPT_SoVITS" / "pretrained_models" / "sv" / "pretrained_eres2netv2w24s4ep4.ckpt"
)

# The model is trained on 16 kHz Kaldi filterbanks with dithering disabled, so the
# features must be built the same way or the embedding drifts.
SAMPLE_RATE = 16000
NUM_MEL_BINS = 80


@lru_cache(maxsize=1)
def _load_architecture():
    """Import ``ERes2NetV2`` and its Kaldi feature helper from the vendored tree.

    Imported by file path rather than by package name because the vendored modules
    import each other flatly (``import pooling_layers``), so the directory itself has
    to be on ``sys.path``.
    """
    directory = str(ERES2NET_DIR)
    if not Path(directory).is_dir():
        raise FileNotFoundError(f"ERes2NetV2 目录不存在：{directory}")
    if directory not in sys.path:
        sys.path.insert(0, directory)

    import kaldi as Kaldi
    from ERes2NetV2 import ERes2NetV2

    return ERes2NetV2, Kaldi


class SpeakerEmbedder:
    """Loads ERes2NetV2 once and turns audio into speaker embeddings."""

    def __init__(self, checkpoint: str | Path | None = None, device: str | None = None) -> None:
        import torch

        path = Path(checkpoint) if checkpoint else DEFAULT_CHECKPOINT
        if not path.is_file():
            raise FileNotFoundError(f"说话人验证模型不存在：{path}")
        ERes2NetV2, self.kaldi = _load_architecture()
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        state = torch.load(str(path), map_location="cpu", weights_only=False)
        model = ERes2NetV2(baseWidth=24, scale=4, expansion=4)
        model.load_state_dict(state)
        model.eval()
        self.model = model.to(self.device)

    def embed_audio(self, audio: np.ndarray, rate: int) -> np.ndarray:
        """Return one L2-normalisable embedding for a mono waveform."""
        import librosa
        import torch

        if rate != SAMPLE_RATE:
            audio = librosa.resample(np.asarray(audio, dtype=np.float32), orig_sr=rate, target_sr=SAMPLE_RATE)
        waveform = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32)).to(self.device)
        with torch.no_grad():
            # dither=0 keeps the features deterministic run to run.
            # ``fbank`` returns (T, F); the model's forward3 expects (B, T, F), so the
            # batch dimension is added here. The vendored sv.py gets the same shape by
            # stacking a list of per-utterance features.
            feat = self.kaldi.fbank(
                waveform.unsqueeze(0), num_mel_bins=NUM_MEL_BINS, sample_frequency=SAMPLE_RATE, dither=0
            ).unsqueeze(0)
            embedding = self.model.forward3(feat)
        return embedding.squeeze(0).float().cpu().numpy()

    def embed_file(self, path: str | Path) -> np.ndarray:
        import soundfile as sf

        audio, rate = sf.read(str(path), dtype="float32", always_2d=True)
        mono = audio.mean(axis=1) if audio.shape[1] > 1 else audio[:, 0]
        return self.embed_audio(mono, rate)


def cosine_similarity(left: np.ndarray, right: np.ndarray) -> float:
    """Cosine similarity between two embeddings, computed explicitly.

    Not using a library dot-product helper because the number is the headline result
    of every timbre claim in this project, so it should be readable.
    """
    left = np.asarray(left, dtype=np.float64).reshape(-1)
    right = np.asarray(right, dtype=np.float64).reshape(-1)
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    if denominator == 0.0:
        return 0.0
    return float(np.dot(left, right) / denominator)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("audio", nargs="+", help="one or more audio files")
    parser.add_argument("--checkpoint", default=str(DEFAULT_CHECKPOINT))
    parser.add_argument("--device", default=None)
    args = parser.parse_args()

    embedder = SpeakerEmbedder(args.checkpoint, args.device)
    print(f"device: {embedder.device}")
    embeddings = {}
    for raw in args.audio:
        embedding = embedder.embed_file(raw)
        embeddings[raw] = embedding
        print(f"  {Path(raw).name:<44} dim={embedding.shape[0]} norm={np.linalg.norm(embedding):.3f}")

    names = list(embeddings)
    if len(names) > 1:
        print("\ncosine similarity")
        for index, left in enumerate(names):
            for right in names[index + 1:]:
                score = cosine_similarity(embeddings[left], embeddings[right])
                print(f"  {Path(left).name[:28]:<30} vs {Path(right).name[:28]:<30} {score:+.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())