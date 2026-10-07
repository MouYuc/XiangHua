#!/usr/bin/env python3
"""Shared kNN-VC runtime for timbre conversion (speech and singing).

kNN-VC (Baas, van Niekerk & Kamper, Interspeech 2023, arXiv:2305.18975) converts
timbre without training anything: a frozen WavLM-Large encoder turns the source
and the reference into layer-6 features, every source frame is replaced by the
mean of its k nearest reference frames, and a HiFi-GAN trained on WavLM features
vocodes the result back to 16 kHz audio.

Three things are centralised here because the CLI and the WebUI service both
need them, and because the upstream `torch.hub` entry point cannot be used in
this repository:

* **Checkpoints are loaded from disk.** ``third_party/knn-vc/hubconf.py`` calls
  ``torch.hub.load_state_dict_from_url``, which downloads WavLM-Large and the
  vocoder from GitHub on every cold start. Both files are already vendored under
  ``third_party/knn-vc/checkpoints/``, so this module reads them locally and
  never touches the network.
* **Reference audio is encoded in chunks.** WavLM-Large runs full attention, so
  the attention matrix grows with the square of the frame count. A 3.8 minute
  reference is about 11 250 frames at the 50 Hz frame rate, and 16 heads over
  that length needs roughly 8 GB just for one layer's scores. The paper asks for
  up to five minutes of reference speech, so the audio is split into short
  windows and the per-window features are concatenated into one matching pool.
  Chunking a *matching pool* is harmless: it is a bag of frames, not a sequence.
* **Matching sets are cached.** Encoding several minutes of reference audio is
  the slow part of the pipeline; the converted frames are reusable across every
  source utterance for the same speaker.

The frame rate is ``sample_rate / hop_length`` = 16000 / 320 = 50 Hz.
"""

from __future__ import annotations

import sys
from pathlib import Path

import torch
import torch.nn.functional as F
import torchaudio

DEFAULT_KNNVC_ROOT = Path("third_party/knn-vc")
DEFAULT_WAVLM_CKPT = "checkpoints/WavLM-Large.pt"
DEFAULT_HIFIGAN_CKPT = "checkpoints/prematch_g_02500000.pt"

# WavLM/ HiFi-GAN both expect 16 kHz; the matcher hardcodes hop_length = 320.
SAMPLE_RATE = 16000
HOP_LENGTH = 320
FRAME_RATE = SAMPLE_RATE / HOP_LENGTH

# 15 s per encoder window keeps a single attention matrix near 40 MB while
# costing nothing in quality, because windows are pooled into a frame set.
DEFAULT_CHUNK_SECONDS = 15.0

# Windows shorter than this produce too few frames to be worth encoding.
_MIN_CHUNK_FRAMES = 8


def ensure_import_path(root: str | Path = DEFAULT_KNNVC_ROOT) -> Path:
    """Put the vendored kNN-VC package on sys.path and return its root.

    ``matcher.py`` imports ``wavlm``, ``hifigan`` and ``knnvc_utils`` as
    top-level modules, so the repository root itself has to be importable.
    """
    resolved = Path(root).resolve()
    if not resolved.is_dir():
        raise FileNotFoundError(f"kNN-VC root not found: {resolved}")
    if str(resolved) not in sys.path:
        sys.path.insert(0, str(resolved))
    return resolved


def resolve_device(device: str | None = None) -> torch.device:
    """Pick the requested device, defaulting to CUDA when it is available."""
    if device:
        return torch.device(device)
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def load_model(
    root: str | Path = DEFAULT_KNNVC_ROOT,
    device: str | None = None,
    prematched: bool = True,
) -> "object":
    """Assemble the kNN-VC matcher from the vendored checkpoints.

    ``prematched=True`` selects the vocoder trained on pre-matched features,
    which is the best model reported in the paper (WER 6.29 / CER 2.34 on
    LibriSpeech dev-clean) and the one the upstream quickstart uses by default.
    Only that vocoder ships in this checkout; asking for the other one is an
    explicit error rather than a silent network download.
    """
    resolved = ensure_import_path(root)
    from hifigan.models import Generator as HiFiGAN
    from hifigan.utils import AttrDict  # noqa: F401  (imported for parity/typing)
    from matcher import KNeighborsVC
    from wavlm.WavLM import WavLM, WavLMConfig

    import json

    device_obj = resolve_device(device)

    if not prematched:
        raise FileNotFoundError(
            "only the prematched HiFi-GAN vocoder is vendored "
            f"({DEFAULT_HIFIGAN_CKPT}); the non-prematched checkpoint would have to be "
            "downloaded from https://github.com/bshall/knn-vc/releases/tag/v0.1 first"
        )

    wavlm_path = resolved / DEFAULT_WAVLM_CKPT
    hifigan_path = resolved / DEFAULT_HIFIGAN_CKPT
    for path in (wavlm_path, hifigan_path):
        if not path.is_file():
            raise FileNotFoundError(f"missing kNN-VC checkpoint: {path}")

    checkpoint = torch.load(wavlm_path, map_location=device_obj, weights_only=False)
    config = WavLMConfig(checkpoint["cfg"])
    wavlm = WavLM(config)
    wavlm.load_state_dict(checkpoint["model"])
    wavlm = wavlm.to(device_obj).eval()

    with (resolved / "hifigan" / "config_v1_wavlm.json").open(encoding="utf-8") as handle:
        hifigan_config = AttrDict(json.loads(handle.read()))
    hifigan = HiFiGAN(hifigan_config).to(device_obj)
    vocoder_state = torch.load(hifigan_path, map_location=device_obj, weights_only=False)
    hifigan.load_state_dict(vocoder_state["generator"])
    hifigan.eval()
    hifigan.remove_weight_norm()

    return KNeighborsVC(wavlm, hifigan, hifigan_config, device=str(device_obj))


def load_audio_16k(path: str | Path) -> torch.Tensor:
    """Load a waveform as mono 16 kHz, shape (1, T).

    Mono is enforced by averaging channels: kNN-VC works on a single feature
    stream, and a stereo reference would otherwise be encoded twice.
    """
    waveform, sample_rate = torchaudio.load(str(path))
    if waveform.dim() == 1:
        waveform = waveform[None]
    if waveform.shape[0] > 1:
        waveform = waveform.mean(dim=0, keepdim=True)
    if sample_rate != SAMPLE_RATE:
        waveform = torchaudio.functional.resample(waveform, sample_rate, SAMPLE_RATE)
    return waveform


def chunk_bounds(total: int, chunk_samples: int) -> list[tuple[int, int]]:
    """Split ``total`` samples into non-overlapping windows."""
    return [(start, min(start + chunk_samples, total)) for start in range(0, total, chunk_samples)]


def features_for_waveform(
    model,
    waveform: torch.Tensor,
    chunk_seconds: float = DEFAULT_CHUNK_SECONDS,
    vad_trigger_level: float = 7.0,
    progress: bool = False,
) -> torch.Tensor:
    """Encode a waveform into WavLM layer-6 features, one chunk at a time.

    VAD trimming runs per chunk, which additionally drops silence at every
    window boundary. For a matching pool that is desirable: silent frames would
    otherwise win nearest-neighbour lookups for quiet source frames and drag the
    converted timbre toward silence.
    """
    chunk_samples = max(int(chunk_seconds * SAMPLE_RATE), _MIN_CHUNK_FRAMES * HOP_LENGTH)
    waveform = waveform.to(model.device)
    pieces: list[torch.Tensor] = []
    bounds = chunk_bounds(waveform.shape[-1], chunk_samples)
    for index, (start, end) in enumerate(bounds):
        piece = waveform[..., start:end]
        if piece.shape[-1] < HOP_LENGTH * 4:
            continue
        features = model.get_features(piece, vad_trigger_level=vad_trigger_level)
        if features.numel():
            pieces.append(features)
        if progress:
            print(f"    chunk {index + 1}/{len(bounds)}: {piece.shape[-1] / SAMPLE_RATE:.1f}s "
                  f"-> {features.shape[0]} frames")
    if not pieces:
        raise ValueError("reference audio produced no usable WavLM frames")
    return torch.cat(pieces, dim=0).cpu()


def build_matching_set(
    model,
    sources: list[str | Path],
    cache: str | Path | None = None,
    chunk_seconds: float = DEFAULT_CHUNK_SECONDS,
    vad_trigger_level: float = 7.0,
    progress: bool = True,
) -> torch.Tensor:
    """Build (or load) the target-speaker frame pool.

    ``cache`` stores the concatenated features as a plain tensor so that the
    expensive encoder pass happens once per speaker instead of once per
    utterance.
    """
    if cache:
        cache_path = Path(cache)
        if cache_path.is_file():
            payload = torch.load(cache_path, map_location="cpu", weights_only=False)
            features = payload["features"] if isinstance(payload, dict) else payload
            if progress:
                print(f"[matching set] cache hit: {cache_path} "
                      f"({features.shape[0]} frames, {features.shape[0] / FRAME_RATE:.1f}s)")
            return features

    pools: list[torch.Tensor] = []
    for source in sources:
        source_path = Path(source)
        if not source_path.is_file():
            raise FileNotFoundError(f"reference audio not found: {source_path}")
        if progress:
            print(f"[matching set] encoding {source_path.name}")
        waveform = load_audio_16k(source_path)
        pools.append(
            features_for_waveform(
                model, waveform, chunk_seconds=chunk_seconds,
                vad_trigger_level=vad_trigger_level, progress=progress,
            )
        )
    features = torch.cat(pools, dim=0)

    if cache:
        cache_path = Path(cache)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(
            {
                "features": features,
                "sources": [str(Path(s)) for s in sources],
                "sample_rate": SAMPLE_RATE,
                "hop_length": HOP_LENGTH,
            },
            cache_path,
        )
        if progress:
            print(f"[matching set] cached {features.shape[0]} frames -> {cache_path}")
    return features


def convert(
    model,
    source: str | Path | torch.Tensor,
    matching_set: torch.Tensor,
    topk: int = 4,
    tgt_loudness_db: float | None = -16.0,
) -> torch.Tensor:
    """Convert one source waveform to the matching set's timbre.

    Returns a (T,) float tensor at 16 kHz. ``topk`` follows the paper's default
    of 4; a smaller k keeps more of the source's own frame variation, which
    matters when the source is sung rather than spoken.
    """
    if isinstance(source, torch.Tensor):
        waveform = source
    else:
        waveform = load_audio_16k(source)
    query = features_for_waveform(
        model, waveform, vad_trigger_level=0.0, progress=False
    ).to(model.device)
    if topk > matching_set.shape[0]:
        raise ValueError(
            f"topk={topk} exceeds the matching set size ({matching_set.shape[0]} frames); "
            "supply more reference audio"
        )
    return model.match(query, matching_set, topk=topk, tgt_loudness_db=tgt_loudness_db)


def save_wav(path: str | Path, waveform: torch.Tensor) -> Path:
    """Write a (T,) or (1, T) tensor as 16 kHz PCM-16."""
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = waveform.detach().cpu()
    if data.dim() == 1:
        data = data[None]
    torchaudio.save(str(out_path), data, SAMPLE_RATE, encoding="PCM_S", bits_per_sample=16)
    return out_path


def speaker_embedding_cosine(a: torch.Tensor, b: torch.Tensor) -> float:
    """Length-normalised cosine between two feature pools, collapsed over time.

    This is the same 'mean-pool then cosine' summary kNN-VC's own prematching
    objective uses (`prematch_dataset.py`), so the number is comparable to the
    similarity the paper optimises rather than an unrelated heuristic.
    """
    left = F.normalize(a.mean(dim=0), dim=0)
    right = F.normalize(b.mean(dim=0), dim=0)
    return float(torch.dot(left, right).item())