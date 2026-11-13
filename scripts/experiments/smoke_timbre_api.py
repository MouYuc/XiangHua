#!/usr/bin/env python3
"""End-to-end smoke test for the personal-timbre WebUI API (kNN-VC).

The WebUI tests in ``backend/tests`` cover the pure helpers, and the Vitest suite
covers the panel logic, but neither proves the *wired* path works: that the
routes exist, that the job snapshots the panel polls really reach ``done``, and
that a finished conversion comes back as something the browser can play.

This script drives the real FastAPI app through Starlette's TestClient using the
real voice profile in ``config.json``:

1. ``GET  /api/timbre/profile``       report corpus + VRAM
2. ``GET  /api/timbre/sources``       list convertible audio
3. ``POST /api/timbre/profile/build`` encode the kNN matching pool, poll to done
4. ``POST /api/timbre/convert-file``  convert a real recording, poll to done
5. ``GET  <result url>``              fetch the produced WAV back over HTTP
6. ``POST /api/timbre/unload``        release VRAM

The first run is the expensive one (WavLM-Large over 64 reference slices, then
over the source); later runs reuse the cached pool.

Usage:
    .venv\\Scripts\\python.exe scripts\\experiments\\smoke_timbre_api.py
    .venv\\Scripts\\python.exe scripts\\experiments\\smoke_timbre_api.py --topk 1 --device cpu
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def poll(client, job_id: str, timeout: float) -> dict:
    """Poll a timbre job until it settles, exactly as the panel's poller does."""
    deadline = time.time() + timeout
    seen: list[str] = []
    while time.time() < deadline:
        response = client.get(f"/api/timbre/status/{job_id}")
        response.raise_for_status()
        job = response.json()
        line = f"{job['status']}/{job.get('phase')}: {job.get('message')}"
        if line != (seen[-1] if seen else None):
            seen.append(line)
            print(f"    {line}")
        if job["status"] in {"done", "failed"}:
            return job
        time.sleep(0.5)
    raise TimeoutError(f"job {job_id} did not settle within {timeout}s; last: {seen[-1:]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topk", type=int, default=4)
    parser.add_argument("--device", default=None, help="default lets the service pick cuda")
    parser.add_argument("--profile-id", default="current")
    parser.add_argument("--source", default=None, help="override the source recording")
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--reuse-cache", action="store_true")
    args = parser.parse_args()

    from fastapi.testclient import TestClient

    from backend.app import create_app

    client = TestClient(create_app())
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        mark = "PASS" if condition else "FAIL"
        print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")
        if not condition:
            failures.append(label)

    print("1. GET /api/timbre/profile")
    profile = client.get(f"/api/timbre/profile?profile_id={args.profile_id}")
    check("profile route answers 200", profile.status_code == 200, profile.text[:200])
    profile = profile.json()
    print(f"    corpus={profile['reference_files']} files, "
          f"cache_ready={profile['cache_ready']}, digest={profile['cache_digest']}, "
          f"cuda={profile['cuda']}")
    check("a reference corpus is configured", profile["reference_files"] > 0)

    print("2. GET /api/timbre/sources")
    sources = client.get("/api/timbre/sources?limit=30")
    check("sources route answers 200", sources.status_code == 200)
    rows = sources.json()["rows"]
    print(f"    {len(rows)} convertible audio files")
    for row in rows[:3]:
        print(f"      {row['origin']:<10} {row['label']}")

    print("3. POST /api/timbre/profile/build")
    if args.reuse_cache and profile["cache_ready"]:
        print("    skipped (--reuse-cache and the pool is already cached)")
    else:
        started = time.time()
        built = client.post(
            "/api/timbre/profile/build",
            json={"profile_id": args.profile_id, "device": args.device},
        )
        check("build route answers 200", built.status_code == 200, built.text[:300])
        job = poll(client, built.json()["job_id"], args.timeout)
        check("matching pool built", job["status"] == "done", job.get("error", ""))
        if job["status"] == "done":
            result = job["result"]
            print(f"    {result['frames']} frames / {result['seconds']}s "
                  f"from {result['reference_files']} files in {time.time() - started:.1f}s")
            check("pool has frames", result["frames"] > 0)

    source = Path(args.source) if args.source else None
    if source is None:
        candidates = [Path(row["path"]) for row in rows if Path(row["path"]).is_file()]
        if not candidates:
            check("a source recording is available", False, "no synthesis history and no uploads")
            return 1
        source = candidates[0]
    check("source recording exists", source.is_file(), str(source))
    print(f"    source: {source}")

    print("4. POST /api/timbre/convert-file")
    started = time.time()
    converted = client.post(
        "/api/timbre/convert-file",
        json={
            "source_path": str(source),
            "profile_id": args.profile_id,
            "topk": args.topk,
            "device": args.device,
        },
    )
    check("convert route answers 200", converted.status_code == 200, converted.text[:300])
    job = poll(client, converted.json()["job_id"], args.timeout)
    check("conversion finished", job["status"] == "done", job.get("error", ""))

    result = job.get("result") or {}
    if job["status"] == "done":
        print(f"    {result['file_name']}: {result['duration']}s audio in "
              f"{result['seconds_taken']}s (topk={result['topk']}, "
              f"pool={result['matching_frames']} frames, wall={time.time() - started:.1f}s)")
        check("result exposes a browser URL", bool(result.get("url")), str(result))
        check("result reports a duration", result.get("duration", 0) > 0)

        print("5. GET <result url>")
        fetched = client.get(result["url"])
        check("produced audio is downloadable", fetched.status_code == 200,
              f"status={fetched.status_code}")
        check("produced audio is a RIFF/WAVE payload", fetched.content[:4] == b"RIFF",
              f"first bytes={fetched.content[:4]!r}")
        print(f"    {len(fetched.content)} bytes over HTTP")

        # kNN-VC rebuilds the waveform from source-derived features, so the length
        # must track the input; a large drift would mean frames were dropped.
        import io

        import soundfile as sf

        produced, produced_rate = sf.read(io.BytesIO(fetched.content), dtype="float32")
        original, original_rate = sf.read(str(source), dtype="float32")
        produced_seconds = produced.shape[0] / produced_rate
        original_seconds = original.shape[0] / original_rate
        drift = abs(produced_seconds - original_seconds)
        print(f"    source {original_seconds:.2f}s @ {original_rate} Hz -> "
              f"output {produced_seconds:.2f}s @ {produced_rate} Hz (drift {drift:.2f}s)")
        check("output is resampled to the model's 16 kHz rate", produced_rate == 16000,
              f"{produced_rate} Hz")
        check("output length tracks the source (drift < 0.5s)", drift < 0.5,
              f"drift {drift:.2f}s")
        check("output reports the same duration it contains",
              abs(float(result["duration"]) - produced_seconds) < 0.1,
              f"reported {result['duration']}s, decoded {produced_seconds:.2f}s")

    print("6. POST /api/timbre/unload")
    unloaded = client.post("/api/timbre/unload")
    check("unload route answers 200", unloaded.status_code == 200)
    print(f"    unloaded={unloaded.json().get('unloaded')}, "
          f"free_gb={unloaded.json().get('free_gb')}")

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())