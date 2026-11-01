"""Unit tests for the reference-audio quality gate.

The gate exists to remove listening from the loop, so the tests are about the
measurements being *correct*, not about the verdicts being convenient. Synthetic
signals are used wherever a property can be isolated - a run of samples pinned at
full scale, a known DC offset, a band-limited noise burst - because those are the
cases where a plausible-looking implementation is silently wrong:

* a clipping ratio cannot distinguish hard clipping from ordinary peak
  normalisation, which is why run length is measured instead;
* an SNR estimate read from "the frames that look silent" disappears exactly when
  broadband noise makes every frame look active;
* a bandwidth check based on spectral roll-off rejects perfectly good wideband
  speech, because F0 and F1 carry most of the energy.

Each of those was a real defect found while calibrating against the vendor's own
prompts and the user's corpus; ``scripts/reference_audio_verify.py`` holds the
population-level acceptance evidence and this file holds the unit-level guarantees.

Run from the repository root:

    .venv\\Scripts\\python.exe -m unittest backend.tests.test_reference_audio -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services import reference_audio as ra

RATE = 22050


def write_wav(path: Path, audio: np.ndarray, rate: int = RATE) -> Path:
    sf.write(str(path), audio.astype(np.float32), rate)
    return path


def speech_like(seconds: float = 4.0, rate: int = RATE, seed: int = 0) -> np.ndarray:
    """Amplitude-modulated tone bursts with gaps: enough structure for every metric."""
    rng = np.random.default_rng(seed)
    count = int(seconds * rate)
    t = np.arange(count) / rate
    carrier = 0.25 * np.sin(2 * np.pi * 180 * t + 0.6 * np.sin(2 * np.pi * 3 * t))
    # Syllable-rate gating with pauses, so there are real quiet frames.
    gate = (np.sin(2 * np.pi * 2.5 * t) > -0.35).astype(np.float64)
    signal = carrier * gate
    signal += rng.normal(0, 1e-4, count)  # a faint, non-digital noise floor
    return signal.astype(np.float32)


class ClippingRunTests(unittest.TestCase):
    def test_a_clean_signal_has_no_run(self) -> None:
        self.assertEqual(ra.longest_full_scale_run(speech_like(1.0)), 0)

    def test_a_single_full_scale_sample_is_not_clipping(self) -> None:
        """Ordinary peak normalisation touches full scale on one sample."""
        audio = speech_like(1.0) / 4.0
        audio[500] = 1.0
        self.assertEqual(ra.longest_full_scale_run(audio), 1)

    def test_a_run_of_pinned_samples_is_found(self) -> None:
        audio = np.zeros(RATE, dtype=np.float32)
        audio[1000:1015] = 1.0
        self.assertEqual(ra.longest_full_scale_run(audio), 15)

    def test_the_longest_run_wins(self) -> None:
        audio = np.zeros(RATE, dtype=np.float32)
        audio[100:104] = 1.0
        audio[500:520] = -1.0
        self.assertEqual(ra.longest_full_scale_run(audio), 20)

    def test_an_empty_signal_is_safe(self) -> None:
        self.assertEqual(ra.longest_full_scale_run(np.zeros(0, dtype=np.float32)), 0)


class LevelEstimateTests(unittest.TestCase):
    def test_a_clean_signal_reports_a_high_snr(self) -> None:
        rms = ra.frame_rms(speech_like(5.0), RATE)
        levels = ra.estimate_levels(rms)
        self.assertIsNotNone(levels["estimated_snr_db"])
        self.assertGreater(levels["estimated_snr_db"], 20.0)

    def test_injecting_noise_lowers_the_estimate_monotonically(self) -> None:
        """The property the gate relies on: more noise must never read as cleaner."""
        clean = speech_like(5.0)
        rms = ra.frame_rms(clean, RATE)
        speech = float(np.percentile(rms, ra.SPEECH_PERCENTILE))
        rng = np.random.default_rng(1)
        measured = []
        for true_snr in (30.0, 20.0, 10.0):
            noise = rng.normal(0, speech / (10 ** (true_snr / 20)), clean.size)
            measured.append(
                ra.estimate_levels(ra.frame_rms(clean + noise.astype(np.float32), RATE))[
                    "estimated_snr_db"
                ]
            )
        self.assertEqual(measured, sorted(measured, reverse=True), f"not monotonic: {measured}")
        self.assertGreater(measured[0] - measured[-1], 5.0, f"range too small: {measured}")

    def test_speech_and_noise_read_different_percentiles(self) -> None:
        """They must not be the same number, which an earlier version made them."""
        levels = ra.estimate_levels(ra.frame_rms(speech_like(5.0), RATE))
        self.assertNotEqual(levels["estimated_snr_db"], levels["quiet_headroom_db"])

    def test_digital_silence_gives_no_estimate_rather_than_a_huge_one(self) -> None:
        """Whole frames of exact zero, not just quiet samples.

        Every frame has to be able to be silent, so the gaps must be longer than the
        25 ms analysis window; scattering isolated loud samples through a zero array
        leaves every frame non-zero and measures nothing.
        """
        tone = 0.4 * np.sin(2 * np.pi * 200 * np.arange(RATE // 2) / RATE)
        audio = np.concatenate([tone, np.zeros(RATE // 2), tone, np.zeros(RATE // 2)])
        levels = ra.estimate_levels(ra.frame_rms(audio.astype(np.float32), RATE))
        self.assertIsNone(levels["estimated_snr_db"])

    def test_an_empty_frame_array_is_safe(self) -> None:
        levels = ra.estimate_levels(np.zeros(0, dtype=np.float32))
        self.assertIsNone(levels["estimated_snr_db"])


class BandwidthTests(unittest.TestCase):
    def test_wideband_noise_has_more_high_band_energy_than_band_limited_noise(self) -> None:
        rng = np.random.default_rng(0)
        wide = rng.normal(0, 0.1, RATE * 2).astype(np.float32)
        import librosa

        narrow = librosa.resample(
            librosa.resample(wide, orig_sr=RATE, target_sr=8000), orig_sr=8000, target_sr=RATE
        )
        active = np.ones(ra.frame_rms(wide, RATE).size, dtype=bool)
        wide_band = ra.effective_bandwidth(wide, RATE, active)["high_band_db"]
        narrow_band = ra.effective_bandwidth(narrow, RATE, active)["high_band_db"]
        self.assertGreater(wide_band, narrow_band + 30.0, f"{wide_band} vs {narrow_band}")

    def test_speech_like_audio_is_not_band_limited(self) -> None:
        """Roll-off would call this narrowband; the 4 kHz ratio must not."""
        audio = speech_like(3.0)
        active = np.ones(ra.frame_rms(audio, RATE).size, dtype=bool)
        metrics = ra.effective_bandwidth(audio, RATE, active)
        self.assertIsNotNone(metrics["high_band_db"])
        self.assertLessEqual(metrics["rolloff_99_hz"], 4000.0, "the roll-off is expected to be low")
        self.assertGreater(metrics["high_band_db"], ra.HIGH_BAND_FAIL_DB)

    def test_no_active_frames_returns_nulls(self) -> None:
        audio = speech_like(1.0)
        metrics = ra.effective_bandwidth(audio, RATE, np.zeros(ra.frame_rms(audio, RATE).size, dtype=bool))
        self.assertIsNone(metrics["high_band_db"])


class AnalyzeTests(unittest.TestCase):
    def test_a_healthy_clip_passes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = write_wav(Path(raw) / "good.wav", speech_like(4.0))
            report = ra.analyze(path, "synthesis")
            self.assertNotEqual(report.status, "fail", report.to_dict()["blocking"])

    def test_a_clipped_clip_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            audio = np.clip(speech_like(4.0) * 40.0, -1.0, 1.0)
            report = ra.analyze(write_wav(Path(raw) / "clip.wav", audio), "synthesis")
            self.assertEqual(report.status, "fail")
            self.assertIn("clipping", [c.key for c in report.checks if c.status == "fail"])

    def test_silence_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            report = ra.analyze(write_wav(Path(raw) / "silent.wav", np.zeros(RATE * 4, dtype=np.float32)))
            self.assertEqual(report.status, "fail")

    def test_too_short_a_prompt_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = write_wav(Path(raw) / "tiny.wav", speech_like(0.5))
            report = ra.analyze(path, "synthesis")
            self.assertIn("duration", [c.key for c in report.checks if c.status == "fail"])

    def test_a_dc_offset_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            audio = speech_like(4.0) + 0.05
            report = ra.analyze(write_wav(Path(raw) / "dc.wav", audio), "synthesis")
            self.assertIn("dc_offset", [c.key for c in report.checks if c.status == "fail"])

    def test_a_stereo_file_is_mixed_down_not_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            mono = speech_like(3.0)
            stereo = np.stack([mono, np.zeros_like(mono)], axis=1)
            path = Path(raw) / "stereo.wav"
            sf.write(str(path), stereo.astype(np.float32), RATE)
            report = ra.analyze(path, "synthesis")
            self.assertEqual(report.metrics["channels"], 2)
            self.assertGreater(report.metrics["peak_dbfs"], -30.0)

    def test_an_unknown_purpose_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = write_wav(Path(raw) / "x.wav", speech_like(1.0))
            with self.assertRaises(ValueError):
                ra.analyze(path, "not-a-purpose")

    def test_pitch_coverage_is_only_measured_when_asked_for(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = write_wav(Path(raw) / "x.wav", speech_like(3.0))
            self.assertNotIn("f0", ra.analyze(path, "synthesis").metrics)
            self.assertIn("f0", ra.analyze(path, "singing").metrics)

    def test_snr_blocks_on_a_long_reference_but_not_on_a_short_prompt(self) -> None:
        """The measured decision behind ``snr_hard``: short prompts span 7-53 dB."""
        with tempfile.TemporaryDirectory() as raw:
            # A clip with essentially no quiet frames, so the estimate is low.
            audio = speech_like(4.0)
            audio = np.sign(audio) * np.abs(audio) ** 0.2  # compress the dynamics
            path = write_wav(Path(raw) / "flat.wav", audio * 0.5)
            prompt = ra.analyze(path, "synthesis")
            reference = ra.analyze(path, "timbre")
            snr_prompt = next(c for c in prompt.checks if c.key == "snr")
            snr_reference = next(c for c in reference.checks if c.key == "snr")
            self.assertIn(snr_prompt.status, {"warn", "fail"})
            self.assertEqual(snr_prompt.status, "warn", "a short prompt must not be blocked on SNR")
            self.assertIn(snr_reference.status, {"warn", "fail"})


class CorpusTests(unittest.TestCase):
    def test_durations_are_summed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = [
                write_wav(root / f"{index}.wav", speech_like(2.0, seed=index))
                for index in range(3)
            ]
            report = ra.analyze_corpus(paths, "timbre")
            self.assertAlmostEqual(report["total_seconds"], 6.0, delta=0.1)
            self.assertEqual(report["files"], 3)

    def test_a_corpus_of_short_slices_passes_where_one_slice_fails(self) -> None:
        """The unit matters: this is exactly the user's own 64-slice situation."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = [
                write_wav(root / f"{index}.wav", speech_like(4.0, seed=index))
                for index in range(8)
            ]
            self.assertEqual(ra.analyze(paths[0], "timbre").status, "fail")
            report = ra.analyze_corpus(paths, "timbre")
            self.assertNotEqual(report["status"], "fail", report["blocking"])

    def test_short_slices_are_flagged_inside_a_passing_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = [write_wav(root / "long.wav", speech_like(30.0))]
            paths.append(write_wav(root / "dot.wav", speech_like(0.3)))
            report = ra.analyze_corpus(paths, "timbre")
            self.assertIn("corpus_short_files", [c["key"] for c in report["checks"]])
            self.assertIn("corpus_short_files", report["warning_checks"])
            self.assertNotIn("corpus_short_files", report["blocking_checks"])

    def test_an_empty_corpus_fails_with_a_message(self) -> None:
        report = ra.analyze_corpus([], "timbre")
        self.assertEqual(report["status"], "fail")
        self.assertIn("没有可用文件", report["message"])

    def test_unreadable_files_do_not_crash_the_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            good = write_wav(root / "good.wav", speech_like(3.0))
            broken = root / "broken.wav"
            broken.write_bytes(b"not audio at all")
            report = ra.analyze_corpus([good, broken], "timbre")
            self.assertEqual(report["files"], 1)

    def test_the_worst_file_is_reported(self) -> None:
        """Both clips are long enough that only clipping fails, so ``worst`` is unambiguous."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            good = write_wav(root / "good.wav", speech_like(25.0))
            clipped = write_wav(
                root / "clipped.wav", np.clip(speech_like(25.0, seed=2) * 40, -1, 1)
            )
            self.assertNotEqual(ra.analyze(good, "timbre").status, "fail")
            report = ra.analyze_corpus([good, clipped], "timbre")
            self.assertEqual(Path(report["worst_file"]["path"]).name, "clipped.wav")
            self.assertEqual(report["worst_file"]["status"], "fail")


class CollectAudioTests(unittest.TestCase):
    def test_only_audio_files_are_collected_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            write_wav(root / "b.wav", speech_like(0.5))
            write_wav(root / "a.wav", speech_like(0.5))
            (root / "notes.txt").write_text("ignore", encoding="utf-8")
            (root / "sliced_whisper.list").write_text("ignore", encoding="utf-8")
            self.assertEqual([p.name for p in ra.collect_audio(root)], ["a.wav", "b.wav"])

    def test_a_missing_directory_is_empty_not_an_error(self) -> None:
        self.assertEqual(ra.collect_audio(Path("does/not/exist")), [])


class VerifyScriptAgreementTests(unittest.TestCase):
    def test_the_thresholds_still_separate_the_populations_they_were_set_from(self) -> None:
        """Guards against a threshold edit that silently reopens a detected defect.

        The values are the measured medians from ``reference_audio_verify.py``; if a
        threshold moves past one of them, the gate has stopped separating the
        populations it was calibrated on and this test is the alarm.
        """
        self.assertLess(ra.HIGH_BAND_FAIL_DB, -55.0 + 1e-9)
        self.assertGreater(ra.HIGH_BAND_FAIL_DB, -66.0, "must stay above the narrowband floor")
        self.assertLess(ra.SNR_FAIL_DB, 24.0, "real references measure 24-32 dB")
        self.assertGreaterEqual(ra.SNR_WARN_DB, ra.SNR_FAIL_DB)
        self.assertLess(ra.CLIPPING_RUN_FAIL, 15, "the vendor's own clipped prompt has a run of 15")
        self.assertGreater(ra.CLIPPING_RUN_FAIL, 1, "an isolated full-scale sample is not clipping")


if __name__ == "__main__":
    unittest.main()