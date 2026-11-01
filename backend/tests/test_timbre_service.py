"""Unit tests for the personal-timbre service's own logic.

The kNN-VC path itself is covered by ``scripts/smoke_timbre_api.py``, which drives
the real model end to end. These tests cover everything *around* the model - the
parts that decide which recordings define a voice, whether a cached matching pool
is still valid, and where an upload is allowed to land. Every one of those
decisions is silent when it is wrong: a stale cache converts against the wrong
voice, and a mis-filed upload quietly joins the user's timbre corpus.

Run from the repository root:

    .venv\\Scripts\\python.exe -m unittest backend.tests.test_timbre_service -v
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services import timbre


class CorpusDigestTests(unittest.TestCase):
    def test_size_and_mtime_change_the_digest(self) -> None:
        """Re-recording over an existing filename must invalidate the pool.

        A digest over names alone would keep converting with the previous take's
        frames, which is undetectable by listening.
        """
        with tempfile.TemporaryDirectory() as raw:
            clip = Path(raw) / "ref.wav"
            clip.write_bytes(b"RIFF" + b"\x00" * 64)
            first = timbre.corpus_digest([clip])
            clip.write_bytes(b"RIFF" + b"\x00" * 128)
            self.assertNotEqual(first, timbre.corpus_digest([clip]))

    def test_the_same_corpus_digests_the_same(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            clip = Path(raw) / "ref.wav"
            clip.write_bytes(b"RIFF" + b"\x00" * 64)
            self.assertEqual(timbre.corpus_digest([clip]), timbre.corpus_digest([clip]))

    def test_a_missing_file_does_not_raise(self) -> None:
        """A deleted reference clip must not take the whole status call down."""
        self.assertEqual(timbre.corpus_digest([Path("does/not/exist.wav")]), timbre.corpus_digest([]))

    def test_cache_path_is_scoped_per_profile_and_revision(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            clip = Path(raw) / "ref.wav"
            clip.write_bytes(b"RIFF" + b"\x00" * 64)
            backend = timbre.TimbreBackend()
            first = backend.profile_cache_path("current", [clip])
            second = backend.profile_cache_path("other", [clip])
            self.assertNotEqual(first, second, "two profiles must not share a pool")
            self.assertEqual(first.parent, timbre.PROFILE_CACHE_DIR)
            self.assertTrue(first.name.startswith("current-"))

    def test_unsafe_profile_characters_are_replaced(self) -> None:
        backend = timbre.TimbreBackend()
        path = backend.profile_cache_path("../../etc/passwd", [])
        self.assertEqual(path.parent, timbre.PROFILE_CACHE_DIR)
        self.assertNotIn("/", path.name)
        self.assertNotIn("\\", path.name)


class ReferenceSelectionTests(unittest.TestCase):
    def test_the_slice_corpus_wins_over_the_single_profile_clip(self) -> None:
        """kNN-VC needs minutes of audio; the profile clip is only a fallback."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            slices = root / "custom_data" / "sliced"
            slices.mkdir(parents=True)
            for index in range(3):
                (slices / f"clip{index}.wav").write_bytes(b"RIFF")
            profile = root / "profile.wav"
            profile.write_bytes(b"RIFF")
            chosen = timbre.reference_files(
                "current", {"gpt_sovits_root": str(root), "ref_audio": str(profile)}
            )
            self.assertEqual([path.name for path in chosen], ["clip0.wav", "clip1.wav", "clip2.wav"])

    def test_it_falls_back_to_the_profile_clip(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            profile = Path(raw) / "profile.wav"
            profile.write_bytes(b"RIFF")
            chosen = timbre.reference_files(
                "current", {"gpt_sovits_root": str(Path(raw) / "absent"), "ref_audio": str(profile)}
            )
            self.assertEqual(chosen, [profile])

    def test_non_audio_files_in_the_slice_directory_are_ignored(self) -> None:
        """The slice directory also holds Whisper transcripts and a ``.list``."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            slices = root / "custom_data" / "sliced"
            slices.mkdir(parents=True)
            (slices / "clip.wav").write_bytes(b"RIFF")
            (slices / "sliced_whisper.list").write_text("clip.wav|x|zh|text", encoding="utf-8")
            (slices / "notes.txt").write_text("ignore me", encoding="utf-8")
            chosen = timbre.reference_files("current", {"gpt_sovits_root": str(root)})
            self.assertEqual([path.name for path in chosen], ["clip.wav"])

    def test_no_reference_audio_returns_empty(self) -> None:
        self.assertEqual(timbre.reference_files("current", {}), [])


class SaveSourceTests(unittest.TestCase):
    def test_an_unsupported_extension_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            timbre.save_source("payload.exe", io.BytesIO(b"MZ"))

    def test_a_supported_upload_is_stored_apart_from_the_references(self) -> None:
        """An uploaded song must never drift into the kNN matching pool.

        Both are audio on disk; only the directory separates a conversion source
        from the recordings that define the timbre.
        """
        saved = timbre.save_source("song.wav", io.BytesIO(b"RIFF" + b"\x00" * 32))
        try:
            self.assertEqual(saved.parent, timbre.source_dir())
            self.assertEqual(saved.parent.parent, timbre.OUTPUT_ROOT)
            self.assertEqual(saved.suffix, ".wav")
            self.assertEqual(saved.read_bytes()[:4], b"RIFF")
            # The generated name keeps the user's own, so the picker stays readable.
            self.assertIn("song.wav", saved.name)
            self.assertNotEqual(saved.parent, timbre.TIMBRE_DIR)
        finally:
            saved.unlink(missing_ok=True)

    def test_a_directory_component_in_the_name_is_stripped(self) -> None:
        saved = timbre.save_source("../../escape.wav", io.BytesIO(b"RIFF"))
        try:
            self.assertEqual(saved.parent, timbre.source_dir())
            self.assertEqual(saved.name.count("escape.wav"), 1)
            self.assertNotIn("..", saved.name)
        finally:
            saved.unlink(missing_ok=True)


class SourceListingTests(unittest.TestCase):
    def test_a_colliding_file_name_is_qualified_with_its_directory(self) -> None:
        """Two rows reading the same name are indistinguishable to the user.

        Synthesis writes into per-experiment directories, so collisions are normal
        rather than exceptional.
        """
        rows = [
            {"label": "001_hello.mp3", "path": r"G:\out\alpha\001_hello.mp3"},
            {"label": "001_hello.mp3", "path": r"G:\out\beta\001_hello.mp3"},
            {"label": "002_unique.mp3", "path": r"G:\out\beta\002_unique.mp3"},
        ]
        labelled = timbre.disambiguate_labels(rows)
        self.assertEqual(
            [row["label"] for row in labelled],
            ["alpha/001_hello.mp3", "beta/001_hello.mp3", "002_unique.mp3"],
        )
        self.assertEqual(len({row["label"] for row in labelled}), 3)

    def test_labels_without_a_collision_are_untouched(self) -> None:
        rows = [{"label": "a.mp3", "path": r"G:\out\one\a.mp3"}]
        self.assertEqual(timbre.disambiguate_labels(rows)[0]["label"], "a.mp3")

    def test_an_empty_list_is_fine(self) -> None:
        self.assertEqual(timbre.disambiguate_labels([]), [])


class JobBookkeepingTests(unittest.TestCase):
    def test_an_unknown_job_has_no_snapshot(self) -> None:
        self.assertIsNone(timbre.TimbreBackend().snapshot("timbre_does_not_exist"))

    def test_snapshots_are_copies(self) -> None:
        """The panel polls this while a worker thread mutates it."""
        backend = timbre.TimbreBackend()
        backend._jobs["demo"] = timbre._Job(
            job_id="demo", snapshot={"job_id": "demo", "status": "running"}
        )
        try:
            snapshot = backend.snapshot("demo")
            snapshot["status"] = "tampered"
            self.assertEqual(backend.snapshot("demo")["status"], "running")
        finally:
            backend._jobs.pop("demo", None)

    def test_updates_stamp_the_time_and_keep_the_id(self) -> None:
        backend = timbre.TimbreBackend()
        backend._jobs["demo"] = timbre._Job(
            job_id="demo", snapshot={"job_id": "demo", "status": "running"}
        )
        try:
            backend._update("demo", {"status": "done", "job_id": "spoofed"})
            snapshot = backend.snapshot("demo")
            self.assertEqual(snapshot["status"], "done")
            self.assertEqual(snapshot["job_id"], "demo")
            self.assertIn("updated_at", snapshot)
        finally:
            backend._jobs.pop("demo", None)

    def test_updating_an_unknown_job_is_a_no_op(self) -> None:
        timbre.TimbreBackend()._update("nope", {"status": "done"})

    def test_jobs_are_listed_newest_first(self) -> None:
        backend = timbre.TimbreBackend()
        for name in ("a", "b", "c"):
            backend._jobs[name] = timbre._Job(job_id=name, snapshot={"job_id": name})
        try:
            self.assertEqual([row["job_id"] for row in backend.list_jobs(2)], ["c", "b"])
        finally:
            for name in ("a", "b", "c"):
                backend._jobs.pop(name, None)


class ConstantsTests(unittest.TestCase):
    def test_the_reference_cap_matches_the_paper(self) -> None:
        self.assertEqual(timbre.MAX_REFERENCE_SECONDS, 300.0)

    def test_the_idle_unload_window_is_long_enough_to_keep_the_model_hot(self) -> None:
        self.assertGreaterEqual(timbre.IDLE_UNLOAD_SECONDS, 300.0)
        self.assertLessEqual(timbre.IDLE_UNLOAD_SECONDS, 1800.0)


if __name__ == "__main__":
    unittest.main()