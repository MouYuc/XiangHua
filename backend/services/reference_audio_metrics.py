"""Deterministic DSP measurement primitives for the reference-audio gate.

Everything here measures; nothing here judges. The functions and constants in
this module answer "what is in this clip" — frame RMS, speech spans, effective
bandwidth, level/SNR estimates, clipping runs, F0 coverage — while the verdicts
that compare those measurements against calibrated thresholds live in
:mod:`backend.services.reference_audio` (the public ``reference_audio`` module,
which re-exports every symbol here under its original name).

Two deliberate limits are worth stating, because they are the difference between a
measurement and a guess:

* **Background music and multiple speakers are not detected here.** Doing it
  properly needs an independent speaker-verification model and a diariser, and a
  cheap proxy would produce confident wrong verdicts. The calibration notes
  record the approach for when that is worth building.
* **The SNR estimate is a stated definition, not a standard.** It is the ratio of
  mean power inside detected speech spans to mean power outside them, which is
  transparent and reproducible but is not ITU-T P.56 or WADA-SNR. It is reported
  as an estimate and never as a certified figure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

# Analysis window for the frame-level statistics. 25 ms with a 10 ms hop is the
# standard short-time setting for speech and makes the silence detection agree
# with the ear at syllable boundaries.
FRAME_SECONDS = 0.025
HOP_SECONDS = 0.010

# A frame counts as silence when its RMS is this far below the clip's own peak
# frame. Relative rather than absolute so the gate works on quiet and loud
# recordings alike; 35 dB is the usual speech-activity margin.
SILENCE_FLOOR_DB = 35.0

# Below this "quiet headroom" (median frame level over the 5th percentile) there is
# no resolved noise floor, so the SNR estimate is a conservative lower bound rather
# than a measurement. Reported as a caveat, never as a pass or a fail.
UNRESOLVED_NOISE_HEADROOM_DB = 8.0

AMPLITUDE_FLOOR = 1e-7  # guards log() on digitally silent files


AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".m4a", ".ogg"}


def collect_audio(directory: str | Path) -> list[Path]:
    """List the audio files in a directory, sorted for a reproducible report."""
    root = Path(directory)
    if not root.is_dir():
        return []
    return sorted(
        path for path in root.iterdir()
        if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES
    )


# ------------------------------------------------------------------- analysis ---


def load_mono(path: str | Path) -> tuple[np.ndarray, int]:
    """Read a clip as float32 mono, mixing channels down by average.

    Averaging rather than taking one channel is deliberate: a stereo recording
    with the voice only on one side would otherwise be analysed at half amplitude
    and could read as too quiet.
    """
    audio, rate = sf.read(str(path), dtype="float32", always_2d=True)
    mono = audio.mean(axis=1) if audio.shape[1] > 1 else audio[:, 0]
    return np.ascontiguousarray(mono, dtype=np.float32), int(rate)


def frame_rms(audio: np.ndarray, rate: int) -> np.ndarray:
    """Short-time RMS on a fixed 25 ms / 10 ms grid."""
    frame = max(1, int(round(FRAME_SECONDS * rate)))
    hop = max(1, int(round(HOP_SECONDS * rate)))
    if audio.size < frame:
        return np.array([float(np.sqrt(np.mean(audio**2))) if audio.size else 0.0])
    count = 1 + (audio.size - frame) // hop
    # Strided view instead of a Python loop: 5 minutes of audio is ~30000 frames.
    strides = (audio.strides[0] * hop, audio.strides[0])
    windowed = np.lib.stride_tricks.as_strided(
        audio, shape=(count, frame), strides=strides, writeable=False
    )
    return np.sqrt(np.mean(windowed.astype(np.float64) ** 2, axis=1))


def speech_spans(rms: np.ndarray, rate: int) -> tuple[list[tuple[int, int]], np.ndarray]:
    """Return active frame spans and the boolean activity mask.

    Reproduces ``librosa.effects.split``'s rule - frames above a threshold set
    ``SILENCE_FLOOR_DB`` below the loudest frame - but in numpy so the threshold is
    visible at the call site instead of hidden in a library default.
    """
    if rms.size == 0:
        return [], np.zeros(0, dtype=bool)
    peak = float(rms.max())
    if peak <= AMPLITUDE_FLOOR:
        return [], np.zeros(rms.size, dtype=bool)
    threshold = peak * (10.0 ** (-SILENCE_FLOOR_DB / 20.0))
    active = rms > threshold
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for index, flag in enumerate(active):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            spans.append((start, index))
            start = None
    if start is not None:
        spans.append((start, active.size))
    return spans, active


def effective_bandwidth(audio: np.ndarray, rate: int, active: np.ndarray) -> dict[str, float | None]:
    """Measure how much of the signal actually lives above 4 kHz.

    The sample rate cannot answer "does this clip have high frequencies": audio
    resampled up from an 8 kHz telephone source keeps whatever rate it was saved at
    and still has nothing above 4 kHz, so fricatives are gone and no sampling rate
    restores them.

    A 99%-energy roll-off is *not* the right statistic for speech: F0 and F1 carry
    most of the energy, so a perfectly wideband recording still rolls off around
    2-3 kHz. What separates wideband from upsampled-narrowband is whether there is
    **any** energy in the 4-8 kHz fricative band at all, so that is measured as the
    level of the 4 kHz-and-up band relative to the 0-4 kHz band, in dB. A
    narrowband source upsampled to 44.1 kHz sits at the numerical floor; real
    wideband speech sits tens of dB above it.
    """
    frame = max(1, int(round(FRAME_SECONDS * rate)))
    hop = max(1, int(round(HOP_SECONDS * rate)))
    indices = np.flatnonzero(active) if active.size else np.array([], dtype=int)
    if indices.size == 0:
        return {"rolloff_99_hz": None, "high_band_db": None, "high_band_ratio": None}
    windows: list[np.ndarray] = []
    for index in indices[:: max(1, indices.size // 200)]:  # at most 200 frames
        start = int(index) * hop
        chunk = audio[start:start + frame]
        if chunk.size < frame:
            continue
        windows.append(chunk * np.hanning(frame))
    if not windows:
        return {"rolloff_99_hz": None, "high_band_db": None, "high_band_ratio": None}

    spectrum = np.abs(np.fft.rfft(np.vstack(windows), axis=1)).mean(axis=0)
    power = spectrum**2
    total = float(power.sum())
    if total <= 0:
        return {"rolloff_99_hz": None, "high_band_db": None, "high_band_ratio": None}
    frequencies = np.fft.rfftfreq(frame, d=1.0 / rate)

    cumulative = np.cumsum(power)
    rolloff_index = min(int(np.searchsorted(cumulative, 0.99 * total)), frequencies.size - 1)

    low = power[frequencies < 4000.0].sum()
    high = power[frequencies >= 4000.0].sum()
    high_ratio = float(high / total)
    high_db = (
        round(float(10.0 * np.log10(high / low)), 2)
        if low > 0 and high > 0
        else (None if high <= 0 else 120.0)
    )
    return {
        "rolloff_99_hz": round(float(frequencies[rolloff_index]), 1),
        "high_band_db": high_db,
        "high_band_ratio": round(high_ratio, 6),
    }


def segment_mask(spans: list[tuple[int, int]], hop: int, length: int) -> np.ndarray:
    """Expand frame spans back to a sample-level boolean mask."""
    mask = np.zeros(length, dtype=bool)
    for start, end in spans:
        mask[start * hop: min(end * hop, length)] = True
    return mask


# The noise floor is read from this percentile of the frame RMS distribution rather
# than from detected silent frames. Detecting silence first does not work as a
# noise estimator: broadband noise raises every frame above the silence threshold,
# so the clip reports zero silent frames and the estimate disappears exactly when it
# is needed. Measured on a ground-truth noise sweep in
# the reference-audio calibration measurements.
NOISE_PERCENTILE = 5.0

# Speech level is read from a high percentile rather than the peak: one plosive
# would otherwise set the "speech level" for the whole clip.
SPEECH_PERCENTILE = 70.0


# The SNR estimate is taken in the speech band only. Without this, band-limiting a
# clip lowers its measured SNR even though no noise was added - the band-limited
# copy loses high-frequency speech energy while the low-frequency noise floor stays
# put - so a narrowband clip would be reported as noisy as well as dull, and the
# user would not know which of the two to fix.
LEVEL_ANALYSIS_CUTOFF_HZ = 3800.0


def band_limit(audio: np.ndarray, rate: int, cutoff_hz: float = LEVEL_ANALYSIS_CUTOFF_HZ) -> np.ndarray:
    """Low-pass a copy for level analysis, or return the input unchanged.

    Falls back to the original signal when scipy is unavailable or the sample rate
    is already at or below twice the cutoff, in which case the estimate is simply
    measured full-band and the caller's numbers are unaffected in kind.
    """
    if rate <= 2.0 * cutoff_hz:
        return audio
    try:
        from scipy.signal import butter, sosfiltfilt
    except Exception:
        return audio
    try:
        sos = butter(4, cutoff_hz / (rate / 2.0), btype="low", output="sos")
        return np.ascontiguousarray(sosfiltfilt(sos, audio.astype(np.float64)), dtype=np.float32)
    except Exception:
        return audio


def estimate_levels(rms: np.ndarray) -> dict[str, float | None]:
    """Frame-level speech level, noise floor and the SNR estimate between them.

    A stated definition, not a standard: transparent, reference-free, and only ever
    reported as an estimate. It is deliberately not ITU-T P.56 or WADA-SNR.

    Speech level (70th percentile) and noise floor (5th percentile) are read from
    different points of the same distribution on purpose. An earlier version of
    this function compared the 95th to the 5th percentile for the SNR *and* for a
    separate "dynamic range" metric, which made the two numbers identical by
    construction - a metric that cannot disagree with another metric carries no
    information.
    """
    if rms.size == 0:
        return {
            "speech_level_dbfs": None,
            "noise_floor_dbfs": None,
            "quiet_headroom_db": None,
            "estimated_snr_db": None,
        }
    speech = float(np.percentile(rms, SPEECH_PERCENTILE))
    noise = float(np.percentile(rms, NOISE_PERCENTILE))
    median = float(np.percentile(rms, 50.0))
    def to_db(value: float) -> float | None:
        return round(float(20.0 * np.log10(value)), 2) if value > AMPLITUDE_FLOOR else None
    headroom = to_db(median / noise) if noise > AMPLITUDE_FLOOR and median > 0 else None
    # Digital silence in the gaps puts the noise floor below the file's own
    # resolution; the ratio is then meaningless rather than merely large.
    snr = to_db(speech / noise) if noise > AMPLITUDE_FLOOR and speech > 0 else None
    return {
        "speech_level_dbfs": to_db(speech),
        "noise_floor_dbfs": to_db(noise),
        "quiet_headroom_db": headroom,
        "estimated_snr_db": snr,
    }


def loudness_metrics(audio: np.ndarray) -> dict[str, float]:
    """Peak, RMS and crest factor in dBFS."""
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2))) if audio.size else 0.0
    peak_db = 20.0 * np.log10(max(peak, AMPLITUDE_FLOOR))
    rms_db = 20.0 * np.log10(max(rms, AMPLITUDE_FLOOR))
    return {
        "peak_dbfs": round(float(peak_db), 2),
        "rms_dbfs": round(float(rms_db), 2),
        "crest_db": round(float(peak_db - rms_db), 2),
    }


def longest_full_scale_run(audio: np.ndarray) -> int:
    """Longest run of consecutive samples pinned at full scale.

    This is what separates clipping from ordinary peak normalisation. A file
    normalised so that one or two samples touch full scale is fine and common; a
    file where fifteen samples in a row sit at exactly 1.0 has been hard-clipped and
    the waveform's shape is gone. Counting samples above a threshold cannot tell the
    two apart - the vendor's own Tianjin prompt has only 2% of samples at full scale,
    which reads as a mild ratio, but its longest run is 15, which is unambiguous.
    """
    if audio.size == 0:
        return 0
    flags = np.abs(audio) >= 0.999
    if not flags.any():
        return 0
    padded = np.concatenate(([False], flags, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    if edges.size < 2:
        return 0
    return int((edges[1::2] - edges[0::2]).max())


def f0_summary(audio: np.ndarray, rate: int, max_seconds: float = 60.0) -> dict[str, Any] | None:
    """Voiced ratio and F0 range over at most ``max_seconds`` of the clip.

    Only meaningful for singing references, where the point is whether the clip
    covers enough pitch range to teach the model a register. Probabilistic YIN is
    used because it is deterministic and ships with librosa; it is slow, so the
    analysis window is capped and the cap is reported.
    """
    try:
        import librosa
    except Exception:
        return None
    window = audio[: int(max_seconds * rate)]
    if window.size < rate * 0.5:
        return None
    try:
        f0, voiced_flag, _ = librosa.pyin(
            window.astype(np.float32),
            fmin=librosa.note_to_hz("C2"),
            fmax=librosa.note_to_hz("C7"),
            sr=rate,
            frame_length=2048,
        )
    except Exception:
        return None
    values = f0[np.isfinite(f0)]
    if values.size == 0:
        return None
    return {
        "analyzed_seconds": round(window.size / rate, 2),
        "voiced_ratio": round(float(np.mean(voiced_flag)), 4),
        "f0_median_hz": round(float(np.median(values)), 1),
        "f0_p05_hz": round(float(np.percentile(values, 5)), 1),
        "f0_p95_hz": round(float(np.percentile(values, 95)), 1),
        "f0_range_semitones": round(
            float(12.0 * np.log2(np.percentile(values, 95) / max(np.percentile(values, 5), 1e-6))), 2
        ),
    }
