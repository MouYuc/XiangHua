"""Speaker diarization and sentence slicing for media intake.

This module works on mono float32 audio that has already been decoded. It does
not touch the filesystem, so the expensive signal logic can be tested with
synthetic waveforms and a fake embedding function.

The design deliberately stops at ``who spoke when``. It does **not** try to
separate overlapping speakers or remove background music: doing that badly
would bake separation artefacts into a voice-training corpus. A later optional
vocal-separation stage can be added in front of this module, but the original
slice remains the source of truth.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

try:  # scipy is already a project dependency, but keep the import failure readable.
    from scipy.ndimage import median_filter
except Exception:  # pragma: no cover - only hit in a broken install
    median_filter = None  # type: ignore[assignment]

FRAME_SECONDS = 0.025
HOP_SECONDS = 0.010
GRID_SECONDS = 0.05

MIN_SPEECH_SECONDS = 0.35
MIN_GAP_SECONDS = 0.25
SPEAKER_WINDOW_SECONDS = 2.0
SPEAKER_HOP_SECONDS = 1.0
MIN_WINDOW_SECONDS = 0.45

MIN_SEGMENT_SECONDS = 2.0
MAX_SEGMENT_SECONDS = 10.0
PAD_SECONDS = 0.08
CUT_SEARCH_SECONDS = 0.70

# A cosine distance of 0.35 means roughly 0.65 cosine similarity. ERes2NetV2
# same-speaker pairs are normally well above that, while different speakers sit
# lower. The value is intentionally conservative: over-splitting shows up as
# duplicate speaker cards, which is visible and fixable; merging two speakers
# silently destroys the dataset.
DEFAULT_CLUSTER_DISTANCE = 0.35


@dataclass
class SpeechRegion:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class SpeakerWindow:
    start: float
    end: float
    embedding: np.ndarray
    label: int = -1

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class Turn:
    start: float
    end: float
    speaker_index: int
    confidence: float = 1.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class SpeakerSummary:
    speaker_id: str
    label: str
    duration_seconds: float
    window_count: int
    turn_count: int
    confidence: float


@dataclass
class DiarizationResult:
    regions: list[SpeechRegion]
    windows: list[SpeakerWindow]
    turns: list[Turn]
    speakers: list[SpeakerSummary]

    @property
    def speaker_count(self) -> int:
        return len(self.speakers)


def rms_frames(audio: np.ndarray, rate: int) -> np.ndarray:
    """Short-time RMS on the same 25 ms / 10 ms grid used elsewhere in the repo."""
    frame = max(1, int(round(FRAME_SECONDS * rate)))
    hop = max(1, int(round(HOP_SECONDS * rate)))
    if audio.size == 0:
        return np.zeros(0, dtype=np.float64)
    if audio.size < frame:
        return np.array([float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))])
    count = 1 + (audio.size - frame) // hop
    strides = (audio.strides[0] * hop, audio.strides[0])
    windowed = np.lib.stride_tricks.as_strided(
        audio, shape=(count, frame), strides=strides, writeable=False
    )
    return np.sqrt(np.mean(windowed.astype(np.float64) ** 2, axis=1))


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, flag in enumerate(mask.tolist()):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(mask)))
    return runs


def detect_speech_regions(audio: np.ndarray, rate: int) -> list[SpeechRegion]:
    """Return speech regions using a noise-floor-relative threshold.

    ``reference_audio.speech_spans`` uses a threshold fixed 35 dB below the
    loudest frame. That is fine for a clean prompt but too permissive for a
    movie with music and effects. Here the threshold also has to clear the
    estimated noise floor, which keeps quiet background beds out of the
    diarization windows.
    """
    rms = rms_frames(audio, rate)
    if rms.size == 0:
        return []
    db = 20.0 * np.log10(np.maximum(rms, 1e-9))
    noise_floor = float(np.percentile(db, 15.0))
    loud = float(np.percentile(db, 95.0))
    threshold = max(noise_floor + 12.0, loud - 35.0, -55.0)
    active = db > threshold

    if median_filter is not None and active.size >= 5:
        active = median_filter(active.astype(np.uint8), size=5) > 0

    # Close short pauses so a sentence is not split at every comma.
    min_gap_frames = max(1, int(round(MIN_GAP_SECONDS / HOP_SECONDS)))
    closed = active.copy()
    for start, end in _runs(~active):
        if start > 0 and end < active.size and (end - start) < min_gap_frames:
            closed[start:end] = True
    active = closed

    min_frames = max(1, int(round(MIN_SPEECH_SECONDS / HOP_SECONDS)))
    regions: list[SpeechRegion] = []
    for start, end in _runs(active):
        if end - start < min_frames:
            continue
        regions.append(
            SpeechRegion(
                start=start * HOP_SECONDS,
                end=min(audio.size / rate, end * HOP_SECONDS),
            )
        )
    return regions


def make_windows(regions: Sequence[SpeechRegion], duration: float) -> list[tuple[float, float]]:
    """Split speech regions into overlapping windows for speaker embeddings."""
    windows: list[tuple[float, float]] = []
    for region in regions:
        if region.duration <= SPEAKER_WINDOW_SECONDS + 0.25:
            windows.append((region.start, region.end))
            continue
        cursor = region.start
        while cursor < region.end - MIN_WINDOW_SECONDS:
            end = min(region.end, cursor + SPEAKER_WINDOW_SECONDS)
            if end - cursor >= MIN_WINDOW_SECONDS:
                windows.append((cursor, end))
            cursor += SPEAKER_HOP_SECONDS
        if windows and windows[-1][1] < region.end - 0.1:
            windows.append((max(region.start, region.end - SPEAKER_WINDOW_SECONDS), region.end))
    return [(start, min(end, duration)) for start, end in windows if end - start >= MIN_WINDOW_SECONDS]


def _normalise(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.maximum(norms, 1e-9)


def _merge_small_clusters(matrix: np.ndarray, labels: np.ndarray, min_size: int) -> np.ndarray:
    labels = labels.copy()
    unique, counts = np.unique(labels, return_counts=True)
    large = [int(label) for label, count in zip(unique, counts) if count >= min_size]
    if not large:
        return labels
    centroids = {
        label: _normalise(matrix[labels == label].mean(axis=0, keepdims=True))[0]
        for label in large
    }
    for label, count in zip(unique, counts):
        if count >= min_size:
            continue
        indices = np.flatnonzero(labels == label)
        for index in indices:
            best = max(large, key=lambda candidate: float(np.dot(matrix[index], centroids[candidate])))
            labels[index] = best
    return labels


def cluster_embeddings(
    embeddings: Sequence[np.ndarray],
    *,
    speaker_count: int | None = None,
    distance_threshold: float = DEFAULT_CLUSTER_DISTANCE,
) -> np.ndarray:
    """Cluster speaker embeddings with agglomerative cosine linkage."""
    matrix = np.asarray(list(embeddings), dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape[0] == 0:
        return np.zeros(0, dtype=int)
    matrix = _normalise(matrix)
    if matrix.shape[0] == 1:
        return np.zeros(1, dtype=int)

    from sklearn.cluster import AgglomerativeClustering

    if speaker_count is not None:
        count = max(1, min(int(speaker_count), matrix.shape[0]))
        model = AgglomerativeClustering(n_clusters=count, metric="cosine", linkage="average")
    else:
        model = AgglomerativeClustering(
            n_clusters=None,
            distance_threshold=float(distance_threshold),
            metric="cosine",
            linkage="average",
        )
    labels = model.fit_predict(matrix)
    # A cluster with one lonely window is almost always an outlier (cough, music
    # hit, microphone bump). Attach it to the nearest real cluster instead of
    # presenting it as a speaker.
    return _merge_small_clusters(matrix, labels, min_size=max(2, matrix.shape[0] // 25))


def _fill_missing(labels: np.ndarray) -> np.ndarray:
    """Forward/backward fill -1 gaps, then smooth labels with a median filter."""
    filled = labels.copy()
    if filled.size == 0:
        return filled
    last = -1
    for index in range(filled.size):
        if filled[index] < 0:
            filled[index] = last
        else:
            last = int(filled[index])
    nxt = -1
    for index in range(filled.size - 1, -1, -1):
        if filled[index] < 0:
            filled[index] = nxt
        else:
            nxt = int(filled[index])
    filled[filled < 0] = 0
    if median_filter is not None and filled.size >= 3:
        filled = median_filter(filled, size=3).astype(int)
    return filled


def labels_to_turns(
    windows: Sequence[SpeakerWindow],
    labels: Sequence[int],
    regions: Sequence[SpeechRegion],
    duration: float,
    *,
    min_turn_seconds: float = MIN_SPEECH_SECONDS,
    max_gap_seconds: float = 0.35,
) -> tuple[list[Turn], dict[int, float]]:
    """Turn overlapping window labels into a single speaker timeline."""
    if not windows or duration <= 0:
        return [], {}
    grid_size = max(1, int(math.ceil(duration / GRID_SECONDS)))
    cluster_ids = sorted({int(label) for label in labels})
    index_of = {cluster: index for index, cluster in enumerate(cluster_ids)}
    votes = np.zeros((len(cluster_ids), grid_size), dtype=np.int16)
    for window, label in zip(windows, labels):
        start = max(0, int(window.start / GRID_SECONDS))
        end = min(grid_size, int(math.ceil(window.end / GRID_SECONDS)))
        if end <= start:
            continue
        votes[index_of[int(label)], start:end] += 1

    best = votes.argmax(axis=0)
    has_vote = votes.max(axis=0) > 0
    timeline = np.where(has_vote, best, -1)
    timeline = _fill_missing(timeline)

    active = np.zeros(grid_size, dtype=bool)
    for region in regions:
        start = max(0, int(region.start / GRID_SECONDS))
        end = min(grid_size, int(math.ceil(region.end / GRID_SECONDS)))
        active[start:end] = True
    timeline[~active] = -1

    turns: list[Turn] = []
    for start, end in _runs(timeline >= 0):
        speaker = int(timeline[start])
        if (end - start) * GRID_SECONDS < min_turn_seconds:
            continue
        turns.append(
            Turn(
                start=start * GRID_SECONDS,
                end=min(duration, end * GRID_SECONDS),
                speaker_index=speaker,
            )
        )

    merged: list[Turn] = []
    for turn in turns:
        if merged and merged[-1].speaker_index == turn.speaker_index and turn.start - merged[-1].end <= max_gap_seconds:
            merged[-1].end = turn.end
        else:
            merged.append(turn)

    # Confidence: average share of the winning cluster among all votes.
    confidence: dict[int, float] = {}
    for cluster in cluster_ids:
        column = index_of[cluster]
        rows = votes[column]
        total = votes.sum(axis=0)
        selected = (timeline >= 0) & (best == column)
        if not selected.any():
            continue
        margin = (rows[selected] / np.maximum(total[selected], 1)).mean()
        confidence[int(cluster)] = float(margin)
    return merged, confidence


def merge_short_turns(
    turns: Sequence[Turn],
    *,
    min_seconds: float = MIN_SEGMENT_SECONDS,
    max_gap_seconds: float = 0.8,
) -> list[Turn]:
    """Attach a too-short turn to the same speaker's neighbour when possible.

    A one-second closing word is useful speech, but a one-second training clip is
    below the synthesis prompt floor. Merging it across a short pause keeps the
    word while producing a clip that can actually be used.
    """
    merged: list[Turn] = []
    for turn in turns:
        if (
            merged
            and merged[-1].speaker_index == turn.speaker_index
            and (merged[-1].duration < min_seconds or turn.duration < min_seconds)
            and turn.start - merged[-1].end <= max_gap_seconds
        ):
            merged[-1].end = max(merged[-1].end, turn.end)
        else:
            merged.append(turn)
    return merged


def diarize(
    audio: np.ndarray,
    rate: int,
    embed_audio: Callable[[np.ndarray, int], np.ndarray],
    *,
    speaker_count: int | None = None,
    progress: Callable[[float], None] | None = None,
) -> DiarizationResult:
    """Run VAD, window embeddings and clustering on one mono waveform."""
    duration = audio.size / rate if rate else 0.0
    regions = detect_speech_regions(audio, rate)
    if not regions:
        return DiarizationResult(regions=[], windows=[], turns=[], speakers=[])

    pairs = make_windows(regions, duration)
    windows: list[SpeakerWindow] = []
    for index, (start, end) in enumerate(pairs):
        chunk = audio[int(start * rate): int(end * rate)]
        if chunk.size < int(MIN_WINDOW_SECONDS * rate):
            continue
        embedding = np.asarray(embed_audio(chunk, rate), dtype=np.float64).reshape(-1)
        windows.append(SpeakerWindow(start=start, end=end, embedding=embedding))
        if progress is not None and pairs:
            progress((index + 1) / len(pairs))
    if not windows:
        return DiarizationResult(regions=regions, windows=[], turns=[], speakers=[])

    labels = cluster_embeddings([window.embedding for window in windows], speaker_count=speaker_count)
    turns, confidence = labels_to_turns(windows, labels, regions, duration)
    turns = merge_short_turns(turns)

    totals: dict[int, float] = {}
    counts: dict[int, int] = {}
    turn_counts: dict[int, int] = {}
    for _window, label in zip(windows, labels):
        counts[int(label)] = counts.get(int(label), 0) + 1
    for turn in turns:
        totals[turn.speaker_index] = totals.get(turn.speaker_index, 0.0) + turn.duration
        turn_counts[turn.speaker_index] = turn_counts.get(turn.speaker_index, 0) + 1

    # Rank clusters by total duration so ``spk_01`` is the dominant speaker.
    ranked = sorted(totals, key=lambda key: totals[key], reverse=True)
    rename = {old: new for new, old in enumerate(ranked)}
    for turn in turns:
        turn.speaker_index = rename[turn.speaker_index]

    speakers: list[SpeakerSummary] = []
    for old_index in ranked:
        index = rename[old_index]
        speakers.append(
            SpeakerSummary(
                speaker_id=f"spk_{index + 1:02d}",
                label=f"说话人 {index + 1}",
                duration_seconds=round(float(totals[old_index]), 3),
                window_count=int(counts.get(old_index, 0)),
                turn_count=int(turn_counts.get(old_index, 0)),
                confidence=round(float(confidence.get(old_index, 0.0)), 4),
            )
        )
    return DiarizationResult(regions=regions, windows=windows, turns=turns, speakers=speakers)


def find_low_energy_cut(audio: np.ndarray, rate: int, low: float, high: float) -> float:
    """Pick the lowest-energy frame in a window so a cut lands between words."""
    duration = audio.size / rate if rate else 0.0
    low = max(0.0, min(low, duration))
    high = max(low, min(high, duration))
    if high - low < 0.02:
        return high
    start = int(low * rate)
    end = int(high * rate)
    chunk = audio[start:end]
    values = rms_frames(chunk, rate)
    if values.size == 0:
        return high
    hop = max(1, int(round(HOP_SECONDS * rate)))
    index = int(np.argmin(values))
    return min(high, low + (index * hop) / rate)


def split_turn(
    audio: np.ndarray,
    rate: int,
    turn: Turn,
    *,
    min_seconds: float = MIN_SEGMENT_SECONDS,
    max_seconds: float = MAX_SEGMENT_SECONDS,
) -> list[tuple[float, float]]:
    """Split a long turn into 2-10 s chunks, preferring low-energy cut points."""
    start, end = turn.start, min(turn.end, audio.size / rate)
    if end - start <= max_seconds:
        return [(start, end)] if end > start else []

    chunks: list[tuple[float, float]] = []
    cursor = start
    while end - cursor > max_seconds:
        low = cursor + min_seconds
        high = cursor + max_seconds
        cut = find_low_energy_cut(audio, rate, high - CUT_SEARCH_SECONDS, high + CUT_SEARCH_SECONDS)
        cut = max(low, min(high, cut))
        if cut <= cursor:
            cut = min(end, cursor + max_seconds)
        chunks.append((cursor, cut))
        cursor = cut
    if end - cursor > 0:
        if chunks and end - cursor < min_seconds:
            chunks[-1] = (chunks[-1][0], end)
        else:
            chunks.append((cursor, end))
    return chunks


def segment_bounds(
    start: float,
    end: float,
    duration: float,
    *,
    pad_seconds: float = PAD_SECONDS,
) -> tuple[float, float]:
    """Add a small guard band without crossing the source boundaries."""
    return max(0.0, start - pad_seconds), min(duration, end + pad_seconds)
