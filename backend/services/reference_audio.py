"""Objective reference-audio quality gate.

Every voice feature in this product is conditioned on a reference recording, and a
bad reference poisons all of them at once: a clipped, noisy or band-limited clip
degrades synthesis prosody, the kNN matching pool and timbre conversion together,
and the failure is audible only as "it sounds worse" - which is exactly the kind of
judgement this project has decided not to make by ear.

So the gate is deterministic DSP. It answers three questions per clip:

* **Is the recording usable at all?** Clipping, DC offset, digital silence and
  absurd loudness are hard defects that no amount of training fixes.
* **Is the clean speech long enough for what it will be used for?** The required
  length is not a property of the clip, it is a property of the consumer: a
  synthesis prompt holds one short utterance, while a kNN-VC matching pool needs
  minutes. The caller therefore states a ``purpose``.
* **Does it carry the frequencies speech needs?** A clip upsampled from a
  telephone-band source keeps its sample rate but has no energy above ~4 kHz, so
  fricatives are gone. Measuring the effective bandwidth catches that, which
  looking at the sample rate cannot.

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

Thresholds are calibrated against real audio from a licensed reference-prompt set;
that set is the reference population, because a gate that rejects known-good
prompts is miscalibrated by definition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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

# What each consumer needs from a reference clip. The durations are set from the
# populations these clips actually come from, measured in
# Calibration prompts span 1.99-8.78 s, so a ``synthesis`` lower bound above about two seconds
# would reject known-good prompts - which is miscalibration by definition. The

# kNN-VC bounds come from the paper (arXiv:2305.18975): quality degrades below
# roughly 20-30 s of reference speech and the earlier measurement in this repo found
# benefit saturating around five minutes.
PURPOSES: dict[str, dict[str, Any]] = {
    "synthesis": {
        "label": "合成参考音",
        "min_seconds": 1.5,
        "max_seconds": 20.0,
        "ideal_seconds": (2.0, 10.0),
        "snr_hard": False,
        "why": "零样本合成的提示音频：太短没有足够音色信息，太长会挤占生成预算。",
    },
    "timbre": {
        "label": "音色匹配池参考音",
        "min_seconds": 20.0,
        "max_seconds": 300.0,
        "ideal_seconds": (60.0, 300.0),
        "snr_hard": True,
        "why": "kNN-VC 需要成分钟的参考语音：论文指出不足约 30 秒质量明显退化，约 5 分钟后收益饱和。",
    },
    "singing": {
        "label": "歌声参考音",
        "min_seconds": 15.0,
        "max_seconds": 300.0,
        "ideal_seconds": (30.0, 300.0),
        "snr_hard": True,
        "why": "歌声转换需要覆盖源音频的音高范围，因此对时长要求高于普通配音。",
    },
}

# ``snr_hard`` above is a measured decision, not a convenience. The SNR estimate
# needs enough frames to find a noise floor, and the vendor's own eighteen official
# prompts - the population a synthesis prompt must not be rejected against - span
# 7.4 to 53.4 dB on this estimator because they are only two to nine seconds long.
# Making that number a hard gate would reject a third of the shipped prompts, so for
# short prompts it is advisory. The long-reference population is tight: the user's
# 64-slice corpus measures 24.0 to 32.2 dB, while ground-truth copies with white
# noise injected at 20 dB or worse measure 16.2 dB and below. There the estimate is
# both reliable and separating, so it blocks.
#
# Hard-defect thresholds. Each sits inside a measured gap between real recordings
# and a deliberately degraded copy of them, not at a round number chosen by taste.
CLIPPING_RATIO_FAIL = 0.005   # 0.5% of samples pinned at full scale
CLIPPING_RATIO_WARN = 0.0005
# A run this long at full scale is hard clipping whatever the ratio says. Three is
# the smallest run that cannot plausibly be a rounded peak.
CLIPPING_RUN_FAIL = 3
DC_OFFSET_FAIL = 0.02         # 2% of full scale: a steady thump and wasted headroom
PEAK_DBFS_FAIL = -40.0        # below this the clip is mostly the noise floor.
# A peak at or near full scale is *not* itself a defect - both real populations
# contain normalised files - so peak is not warned on. Only actual clipping is, and
# that is measured directly by run length.
RMS_DBFS_WARN = -30.0
RMS_DBFS_FAIL = -34.0
SILENCE_RATIO_FAIL = 0.90     # 90% silence means there is almost no speech in it
SILENCE_RATIO_WARN = 0.60

# Effective bandwidth, measured as the level of the 4 kHz-and-up fricative band
# relative to the band below it. Ground-truth narrowband audio (real speech
# resampled to 8 kHz and back, which is the exact defect being detected) measures
# -66 to -78 dB; every real wideband recording measured -12 to -42 dB. The bounds
# sit in that gap.
HIGH_BAND_FAIL_DB = -55.0
HIGH_BAND_WARN_DB = -38.0

# SNR estimate bounds, in the same spirit. See the ``snr_hard`` note above for why
# they block only on long references.
#
# The estimator compresses true SNR, which is a property of measuring a ratio of
# percentiles rather than a calibrated noise power: measured on the concatenated
# slice corpus, injecting white noise at a true 20 dB reads 23.6 dB and at a true
# 10 dB reads 15.2 dB. The fail line therefore corresponds to roughly 15 dB of true
# SNR, which is what it is documented as. The warning line is set so that a corpus
# whose noise is audible in the timbre - the 20 dB case - is flagged rather than
# silently accepted.
SNR_FAIL_DB = 18.0
SNR_WARN_DB = 26.0
SNR_CEILING_DB = 45.0

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


@dataclass
class Check:
    """One pass/warn/fail verdict with the reasoning a user can act on."""

    key: str
    label: str
    status: str  # "ok" | "warn" | "fail"
    measured: Any
    threshold: str
    message: str


@dataclass
class ReferenceReport:
    """The full gate result for one clip."""

    path: str
    purpose: str
    status: str
    duration_seconds: float
    sample_rate: int
    channels: int
    metrics: dict[str, Any] = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "purpose": self.purpose,
            "purpose_label": PURPOSES.get(self.purpose, {}).get("label", self.purpose),
            "status": self.status,
            "duration_seconds": self.duration_seconds,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "metrics": self.metrics,
            "checks": [
                {
                    "key": check.key,
                    "label": check.label,
                    "status": check.status,
                    "measured": check.measured,
                    "threshold": check.threshold,
                    "message": check.message,
                }
                for check in self.checks
            ],
            "notes": self.notes,
            "blocking": [check.message for check in self.checks if check.status == "fail"],
            "warnings": [check.message for check in self.checks if check.status == "warn"],
        }


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

# When the 5th percentile sits this close to the median there is no resolved noise
# floor, so the estimate below is a conservative *lower bound* rather than a
# measurement. Reported so the reader knows which one they are looking at.
UNRESOLVED_NOISE_HEADROOM_DB = 8.0


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
    to_db = lambda value: round(float(20.0 * np.log10(value)), 2) if value > AMPLITUDE_FLOOR else None
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


# ----------------------------------------------------------------------- gate ---


def _status(ok: bool, warn: bool) -> str:
    if not ok:
        return "fail"
    return "warn" if warn else "ok"


def analyze(
    path: str | Path,
    purpose: str = "synthesis",
    *,
    with_f0: bool | None = None,
) -> ReferenceReport:
    """Measure one clip and grade it for the stated purpose."""
    if purpose not in PURPOSES:
        raise ValueError(f"未知用途：{purpose}（可选：{', '.join(PURPOSES)}）")
    source = Path(path)
    audio, rate = load_mono(source)
    spec = PURPOSES[purpose]
    duration = round(audio.size / rate, 3) if rate else 0.0
    info_channels = sf.info(str(source)).channels

    rms = frame_rms(audio, rate)
    spans, active = speech_spans(rms, rate)
    # Silence is counted on the analysis frame grid, not by expanding the spans
    # back to samples: a sample-level mask misses the tail shorter than one hop and
    # would report a tightly cut prompt as ~1% silent no matter how it sounds.
    silence_ratio = 1.0 - (float(active.mean()) if active.size else 0.0)
    # Levels come from a band-limited copy so the noise estimate does not move when
    # high frequencies are missing; see LEVEL_ANALYSIS_CUTOFF_HZ.
    levels = estimate_levels(frame_rms(band_limit(audio, rate), rate))
    snr = levels["estimated_snr_db"]
    headroom = levels["quiet_headroom_db"]
    bandwidth = effective_bandwidth(audio, rate, active)
    loudness = loudness_metrics(audio)
    clipping_ratio = float(np.mean(np.abs(audio) >= 0.999)) if audio.size else 0.0
    clipping_run = longest_full_scale_run(audio)
    dc_offset = float(np.mean(audio)) if audio.size else 0.0

    metrics: dict[str, Any] = {
        "duration_seconds": duration,
        "sample_rate": rate,
        "channels": info_channels,
        "clipping_ratio": round(clipping_ratio, 6),
        "clipping_run": clipping_run,
        "dc_offset": round(dc_offset, 6),
        "silence_ratio": round(silence_ratio, 4),
        "speech_spans": len(spans),
        **levels,
        "nyquist_hz": rate / 2.0,
        **bandwidth,
        **loudness,
    }

    want_f0 = purpose == "singing" if with_f0 is None else with_f0
    if want_f0:
        f0 = f0_summary(audio, rate)
        if f0 is not None:
            metrics["f0"] = f0

    checks: list[Check] = []

    def add(key: str, label: str, status: str, measured: Any, threshold: str, message: str) -> None:
        checks.append(Check(key, label, status, measured, threshold, message))

    # --- hard defects -------------------------------------------------------
    add(
        "duration",
        "时长",
        _status(
            duration >= spec["min_seconds"],
            duration < spec["ideal_seconds"][0] or duration > spec["ideal_seconds"][1],
        ),
        f"{duration:.2f}s",
        f"{spec['min_seconds']:.0f}–{spec['max_seconds']:.0f}s（理想 {spec['ideal_seconds'][0]:.0f}–{spec['ideal_seconds'][1]:.0f}s）",
        (
            f"太短：{duration:.2f}s，{spec['label']}至少需要 {spec['min_seconds']:.0f}s。{spec['why']}"
            if duration < spec["min_seconds"]
            else (
                f"偏短：{duration:.2f}s，建议 {spec['ideal_seconds'][0]:.0f}s 以上。{spec['why']}"
                if duration < spec["ideal_seconds"][0]
                else (
                    f"过长：{duration:.2f}s，超过 {spec['max_seconds']:.0f}s 后收益饱和，只会拖慢编码。"
                    if duration > spec["max_seconds"]
                    else f"时长合适：{duration:.2f}s。"
                )
            )
        ),
    )
    add(
        "clipping",
        "削波",
        _status(
            clipping_ratio < CLIPPING_RATIO_FAIL and clipping_run < CLIPPING_RUN_FAIL,
            clipping_ratio > CLIPPING_RATIO_WARN,
        ),
        f"{clipping_ratio * 100:.3f}% 样本贴顶，最长连续 {clipping_run} 个样本",
        f"< {CLIPPING_RATIO_FAIL * 100:.2f}% 且连续 < {CLIPPING_RUN_FAIL} 个样本",
        (
            f"有 {clipping_run} 个连续样本停在满量程，属于硬削波：波形已经被切平，"
            "产生的谐波失真无法还原，请重新录制并降低增益。"
            if clipping_run >= CLIPPING_RUN_FAIL
            else (
                f"有 {clipping_ratio * 100:.2f}% 的样本贴顶，建议留 3~6 dB 余量。"
                if clipping_ratio >= CLIPPING_RATIO_FAIL
                else (
                    f"有轻微贴顶（{clipping_ratio * 100:.3f}%），但都是孤立样本、没有连续削波，"
                    "属于正常归一化。"
                    if clipping_ratio > CLIPPING_RATIO_WARN
                    else "没有削波。"
                )
            )
        ),
    )
    add(
        "dc_offset",
        "直流偏置",
        _status(abs(dc_offset) < DC_OFFSET_FAIL, False),
        f"{dc_offset * 100:.3f}% 满量程",
        f"< {DC_OFFSET_FAIL * 100:.0f}%",
        (
            f"直流偏置 {dc_offset * 100:.2f}%，会占用动态范围并带来低频撞击声，建议高通滤波后重录。"
            if abs(dc_offset) >= DC_OFFSET_FAIL
            else "没有明显直流偏置。"
        ),
    )
    add(
        "level",
        "电平",
        _status(
            loudness["peak_dbfs"] > PEAK_DBFS_FAIL and loudness["rms_dbfs"] >= RMS_DBFS_FAIL,
            loudness["rms_dbfs"] < RMS_DBFS_WARN,
        ),
        f"峰值 {loudness['peak_dbfs']:.1f} dBFS / 有效值 {loudness['rms_dbfs']:.1f} dBFS",
        f"峰值 > {PEAK_DBFS_FAIL:.0f} dBFS 且有效值 ≥ {RMS_DBFS_FAIL:.0f} dBFS",
        (
            f"录音电平过低（峰值 {loudness['peak_dbfs']:.1f} dBFS），有效语音很可能被噪声底掩盖。"
            if loudness["peak_dbfs"] <= PEAK_DBFS_FAIL or loudness["rms_dbfs"] < RMS_DBFS_FAIL
            else (
                f"有效值偏低（{loudness['rms_dbfs']:.1f} dBFS），语音离噪声底较近，"
                "归一化会连噪声一起放大。"
                if loudness["rms_dbfs"] < RMS_DBFS_WARN
                else f"电平正常（峰值 {loudness['peak_dbfs']:.1f} dBFS，有效值 {loudness['rms_dbfs']:.1f} dBFS）。"
            )
        ),
    )
    add(
        "silence",
        "静音占比",
        _status(silence_ratio < SILENCE_RATIO_FAIL, silence_ratio > SILENCE_RATIO_WARN),
        f"{silence_ratio * 100:.1f}%",
        f"< {SILENCE_RATIO_FAIL * 100:.0f}%",
        (
            f"整段有 {silence_ratio * 100:.0f}% 是静音，几乎没有可用语音；请检查是否录到了空白段。"
            if silence_ratio >= SILENCE_RATIO_FAIL
            else (
                f"静音占 {silence_ratio * 100:.0f}%，有效语音偏少（参考音编码会把这些静音也算进匹配池）。"
                if silence_ratio > SILENCE_RATIO_WARN
                else f"有效语音占 {100 - silence_ratio * 100:.0f}%。"
            )
        ),
    )
    # A short prompt cannot support a reliable noise-floor estimate, so for that
    # purpose a low number is reported but never blocks. See the ``snr_hard`` note
    # beside PURPOSES for the measurement this is based on.
    snr_blocks = bool(spec.get("snr_hard", True))
    add(
        "snr",
        "信噪比（估计）",
        _status(
            snr is None or snr >= SNR_FAIL_DB or not snr_blocks,
            snr is not None and snr < SNR_WARN_DB,
        ),
        (
            "无法估计" if snr is None
            else (
                f"{snr:.1f} dB（安静段余量 {headroom:.1f} dB）"
                if headroom is not None
                else f"{snr:.1f} dB"
            )
        ),
        f"≥ {SNR_FAIL_DB:.0f} dB（警告线 {SNR_WARN_DB:.0f} dB）",
        (
            "间隙是数字静音，噪声底低于文件本身的量化精度，信噪比无从估计——对参考音来说通常没问题。"
            if snr is None
            else (
                f"估计信噪比只有 {snr:.1f} dB，底噪会和语音一起被学进音色，建议换安静环境重录。"
                if snr < SNR_FAIL_DB and snr_blocks
                else (
                    f"估计信噪比只有 {snr:.1f} dB，偏低。这段音频太短，噪声底估计不可靠，"
                    "所以只提示、不拦截；如果整份参考音都是这个水平，请重录。"
                    if snr < SNR_FAIL_DB
                    else (
                        f"估计信噪比 {snr:.1f} dB，偏低，建议清理环境噪声。"
                        if snr < SNR_WARN_DB
                        else (
                            f"估计信噪比 {snr:.1f} dB（> {SNR_CEILING_DB:.0f} dB 时多半是在测数字静音）。"
                            if snr > SNR_CEILING_DB
                            else (
                                f"估计信噪比 {snr:.1f} dB，数字上干净；但安静段余量只有 "
                                f"{headroom:.1f} dB，说明几乎没有安静帧，这个估计值是保守下限而非精确值。"
                                if headroom is not None and headroom < UNRESOLVED_NOISE_HEADROOM_DB
                                else f"估计信噪比 {snr:.1f} dB，干净。"
                            )
                        )
                    )
                )
            )
        ),
    )
    high_band = metrics["high_band_db"]
    add(
        "bandwidth",
        "高频带宽",
        _status(high_band is None or high_band >= HIGH_BAND_FAIL_DB,
                high_band is not None and high_band < HIGH_BAND_WARN_DB),
        (
            "无法测量" if high_band is None
            else f"4 kHz 以上比以下低 {abs(high_band):.1f} dB"
                 f"（占全带能量 {metrics['high_band_ratio'] * 100:.3f}%，"
                 f"99% 能量到 {metrics['rolloff_99_hz'] or 0:.0f} Hz）"
        ),
        f"≥ {HIGH_BAND_FAIL_DB:.0f} dB（警告线 {HIGH_BAND_WARN_DB:.0f} dB）",
        (
            "没能测到高频带宽（几乎没有语音帧）。"
            if high_band is None
            else (
                f"4 kHz 以上几乎没有能量（比低带低 {abs(high_band):.0f} dB），"
                f"说明这段音频来自窄带来源（电话录音、老录音或已被压成 8 kHz 再升采样），"
                f"摩擦音 s/sh/f 已经不存在，采样率再高也补不回来。"
                if high_band < HIGH_BAND_FAIL_DB
                else (
                    f"4 kHz 以上能量偏少（低 {abs(high_band):.0f} dB），齿音可能发闷。"
                    if high_band < HIGH_BAND_WARN_DB
                    else f"高频带宽充足（4 kHz 以上只低 {abs(high_band):.0f} dB）。"
                )
            )
        ),
    )

    f0 = metrics.get("f0")
    if want_f0 and f0 is not None:
        add(
            "pitch_range",
            "音高覆盖",
            _status(f0["voiced_ratio"] >= 0.30, f0["f0_range_semitones"] < 4.0),
            f"浊音占比 {f0['voiced_ratio'] * 100:.0f}%，F0 {f0['f0_p05_hz']:.0f}–{f0['f0_p95_hz']:.0f} Hz"
            f"（{f0['f0_range_semitones']:.1f} 半音）",
            "浊音占比 ≥ 30%，音域 ≥ 4 半音",
            (
                "几乎没有浊音帧，可能不是人声。"
                if f0["voiced_ratio"] < 0.30
                else (
                    f"音域只有 {f0['f0_range_semitones']:.1f} 半音，参考音的音高范围窄，"
                    "转换到音域更宽的歌声时会显得平。"
                    if f0["f0_range_semitones"] < 4.0
                    else f"音域 {f0['f0_range_semitones']:.1f} 半音，覆盖合理。"
                )
            ),
        )

    notes = [
        "本门禁只做确定性 DSP 测量，不做背景音乐检测，也不做说话人分离——"
        "那需要独立的说话人验证模型与聚类，便宜的替代做法会给出自信的错误结论。",
        "信噪比是「语音段功率 / 间隙段功率」的估计值，不是 ITU-T P.56 或 WADA-SNR 认证值。",
    ]
    if purpose == "timbre" and duration > 300.0:
        notes.append("超过 300 秒的部分不会提升匹配池质量，只会拖慢编码。")

    statuses = {check.status for check in checks}
    status = "fail" if "fail" in statuses else ("warn" if "warn" in statuses else "ok")
    return ReferenceReport(
        path=str(source),
        purpose=purpose,
        status=status,
        duration_seconds=duration,
        sample_rate=rate,
        channels=info_channels,
        metrics=metrics,
        checks=checks,
        notes=notes,
    )


def judge(audio_path: str | Path, purpose: str = "synthesis", **kwargs: Any) -> dict[str, Any]:
    """Convenience wrapper returning the report as a plain dict."""
    return analyze(audio_path, purpose, **kwargs).to_dict()


# A corpus clip shorter than this contributes so little that including it is worse
# than excluding it: it adds frames to the matching pool without adding voice.
MIN_USEFUL_SLICE_SECONDS = 1.0


def analyze_corpus(
    paths: list[str | Path],
    purpose: str = "timbre",
) -> dict[str, Any]:
    """Grade a whole reference corpus rather than one file.

    The ``timbre`` purpose cannot be judged per file: kNN-VC pools all the reference
    audio, and the user's own corpus is 64 slices of two to seven seconds each -
    every one of which fails a twenty-second per-file minimum while the corpus as a
    whole is 236 seconds and entirely healthy. Judging the wrong unit would make the
    gate useless exactly where it matters most.

    Durations add. Everything else is aggregated the honest way for its kind: ratios
    by total duration, levels by pooling the frames that produced them. The worst
    per-file verdict is kept as ``worst_file`` so a single broken slice is still
    visible inside a passing corpus.
    """
    usable = [Path(path) for path in paths if Path(path).is_file()]
    if not usable:
        return {"status": "fail", "files": 0, "message": "参考音频目录里没有可用文件。"}

    per_file: list[ReferenceReport] = []
    for path in usable:
        try:
            per_file.append(analyze(path, purpose))
        except Exception:
            continue
    if not per_file:
        return {"status": "fail", "files": len(usable), "message": "所有参考音频都无法读取。"}

    total_seconds = sum(report.duration_seconds for report in per_file)
    total_bytes = sum(path.stat().st_size for path in usable if path.is_file())

    # Duration-weighted aggregates, so a five-second slice cannot outvote a
    # one-minute one on a ratio.
    def weighted(key: str) -> float | None:
        pairs = [
            (report.metrics[key], report.duration_seconds)
            for report in per_file
            if report.metrics.get(key) is not None
        ]
        if not pairs:
            return None
        weight = sum(duration for _, duration in pairs)
        if weight <= 0:
            return None
        return round(sum(value * duration for value, duration in pairs) / weight, 3)

    # Levels pool over frames, not over clips: averaging dB values per clip would let
    # a quiet two-second slice count as much as a loud one-minute one.
    all_rms: list[np.ndarray] = []
    for report in per_file:
        try:
            audio, rate = load_mono(report.path)
        except Exception:
            continue
        if audio.size:
            all_rms.append(frame_rms(band_limit(audio, rate), rate))
    if all_rms:
        levels = estimate_levels(np.concatenate(all_rms))
    else:
        levels = {
            "speech_level_dbfs": None,
            "noise_floor_dbfs": None,
            "quiet_headroom_db": None,
            "estimated_snr_db": None,
        }

    spec = PURPOSES[purpose]
    short_files = [report for report in per_file if report.duration_seconds < MIN_USEFUL_SLICE_SECONDS]
    snr = levels["estimated_snr_db"]
    high_band = weighted("high_band_db")
    clipping = weighted("clipping_ratio") or 0.0

    checks: list[Check] = [
        Check(
            "total_duration",
            "参考音总时长",
            _status(
                total_seconds >= spec["min_seconds"],
                total_seconds < spec["ideal_seconds"][0] or total_seconds > spec["max_seconds"],
            ),
            f"{total_seconds:.1f}s / {len(per_file)} 个文件",
            f"≥ {spec['min_seconds']:.0f}s（理想 ≥ {spec['ideal_seconds'][0]:.0f}s，上限 {spec['max_seconds']:.0f}s）",
            (
                f"参考音总时长只有 {total_seconds:.1f}s，{spec['label']}至少需要 "
                f"{spec['min_seconds']:.0f}s。{spec['why']}"
                if total_seconds < spec["min_seconds"]
                else (
                    f"参考音总时长 {total_seconds:.1f}s，够用；建议补到 "
                    f"{spec['ideal_seconds'][0]:.0f}s 以上更稳。{spec['why']}"
                    if total_seconds < spec["ideal_seconds"][0]
                    else (
                        f"参考音总时长 {total_seconds:.1f}s 超过 {spec['max_seconds']:.0f}s，"
                        "超出部分不会提升匹配池质量，只会拖慢编码。"
                        if total_seconds > spec["max_seconds"]
                        else f"参考音总时长 {total_seconds:.1f}s，落在理想区间。"
                    )
                )
            ),
        ),
        Check(
            "corpus_snr",
            "语料信噪比（估计）",
            _status(
                snr is None or snr >= SNR_FAIL_DB or not spec.get("snr_hard", True),
                snr is not None and snr < SNR_WARN_DB,
            ),
            "无法估计" if snr is None else f"{snr:.1f} dB（安静段余量 {levels['quiet_headroom_db']} dB）",
            f"≥ {SNR_FAIL_DB:.0f} dB（警告线 {SNR_WARN_DB:.0f} dB）",
            (
                "整份参考音没有可测的噪声底，信噪比无从估计。"
                if snr is None
                else (
                    f"整份参考音估计信噪比 {snr:.1f} dB，底噪会被一起学进音色，建议重录。"
                    if snr < SNR_FAIL_DB
                    else (
                        f"整份参考音估计信噪比 {snr:.1f} dB，处于临界区：底噪已经开始接近语音，"
                        "转换结果可能带上原来的环境声，建议清理后再补录一段。"
                        if snr < SNR_WARN_DB
                        else f"整份参考音估计信噪比 {snr:.1f} dB，干净。"
                    )
                )
            ),
        ),
        Check(
            "corpus_bandwidth",
            "语料高频带宽",
            _status(
                high_band is None or high_band >= HIGH_BAND_FAIL_DB,
                high_band is not None and high_band < HIGH_BAND_WARN_DB,
            ),
            "无法测量" if high_band is None else f"4 kHz 以上平均低 {abs(high_band):.1f} dB",
            f"≥ {HIGH_BAND_FAIL_DB:.0f} dB（警告线 {HIGH_BAND_WARN_DB:.0f} dB）",
            (
                "没能测到高频带宽。"
                if high_band is None
                else (
                    f"整份参考音 4 kHz 以上平均低 {abs(high_band):.0f} dB，来自窄带来源，"
                    "摩擦音已经不存在。"
                    if high_band < HIGH_BAND_FAIL_DB
                    else f"整份参考音高频带宽充足（低 {abs(high_band):.0f} dB）。"
                )
            ),
        ),
        Check(
            "corpus_clipping",
            "语料削波",
            _status(clipping < CLIPPING_RATIO_FAIL, clipping > CLIPPING_RATIO_WARN),
            f"{clipping * 100:.3f}% 样本贴顶（按时长加权）",
            f"< {CLIPPING_RATIO_FAIL * 100:.2f}%",
            (
                "整份参考音里有明显削波，会带来无法还原的谐波失真。"
                if clipping >= CLIPPING_RATIO_FAIL
                else "整份参考音没有明显削波。"
            ),
        ),
    ]
    if short_files:
        checks.append(
            Check(
                "corpus_short_files",
                "过短切片",
                "warn",
                f"{len(short_files)} 个文件不足 {MIN_USEFUL_SLICE_SECONDS:.0f}s",
                f"建议剔除 < {MIN_USEFUL_SLICE_SECONDS:.0f}s 的切片",
                "有切片短到几乎没有语音，留着只会给匹配池加噪声帧，建议删掉。",
            )
        )

    counts = {"ok": 0, "warn": 0, "fail": 0}
    for report in per_file:
        counts[report.status] += 1
    worst = max(per_file, key=lambda report: ["ok", "warn", "fail"].index(report.status))
    statuses = {check.status for check in checks}
    status = "fail" if "fail" in statuses else ("warn" if "warn" in statuses else "ok")
    return {
        "purpose": purpose,
        "purpose_label": spec["label"],
        "status": status,
        "files": len(per_file),
        "total_seconds": round(total_seconds, 2),
        "total_bytes": total_bytes,
        "aggregate": {**levels, "high_band_db": high_band, "clipping_ratio": round(clipping, 6)},
        "per_file_counts": counts,
        "worst_file": {"path": worst.path, "status": worst.status, "checks": [
            {"key": check.key, "status": check.status, "message": check.message}
            for check in worst.checks if check.status != "ok"
        ]},
        "checks": [
            {
                "key": check.key,
                "label": check.label,
                "status": check.status,
                "measured": check.measured,
                "threshold": check.threshold,
                "message": check.message,
            }
            for check in checks
        ],
        "blocking": [check.message for check in checks if check.status == "fail"],
        "warnings": [check.message for check in checks if check.status == "warn"],
        # The keys alongside the messages: a caller displaying text wants the
        # messages, a caller deciding what to do wants the stable identifiers.
        "blocking_checks": [check.key for check in checks if check.status == "fail"],
        "warning_checks": [check.key for check in checks if check.status == "warn"],
        "per_file": [report.to_dict() for report in per_file],
        "notes": [
            "总时长按秒相加；比例类指标按时长加权；电平把各文件的帧汇总后再取分位数，"
            "避免一个 2 秒切片和一段 1 分钟录音等权。",
            "逐文件明细在 per_file 里，worst_file 指出最需要先修的那一个。",
        ],
    }