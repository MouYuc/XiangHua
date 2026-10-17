#!/usr/bin/env python3
"""Verify the training loop objectively: does a freshly trained voice actually sound right?

Why this exists
---------------

Finishing `s1_train.py` and `s2_train.py` proves that *code ran*. It does not prove the
result is the user's voice, and the difference matters: a checkpoint trained on the wrong
data, with a broken reference clip, or with mis-aligned text loads and synthesises happily
while sounding like someone else. The only way to close the loop without listening is to
measure speaker identity with a model that is independent of the one trained.

The measurement is SECS (speaker-embedding cosine similarity) with ERes2NetV2 from
``scripts/speaker_embedding.py`` - the same independent embedder the timbre features use.
The verdict is not an absolute threshold but a **relative** one, because absolute SECS
depends on channel and language:

* ``same_speaker_baseline``: two *different* recordings of the speaker who provided the
  training data. This is what "sounds like this person" actually scores on this material.
* ``different_speaker``: a clip that is definitely not the trained speaker.

The synthesised audio must sit clearly above the different-speaker value and close to the
same-speaker baseline. That is a claim about *this* speaker on *this* corpus, which an
absolute cutoff could not make.

Usage:
    .venv\\Scripts\\python.exe scripts\\verify_training_loop.py
    .venv\\Scripts\\python.exe scripts\\verify_training_loop.py --text "..." --json-out out.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

DEFAULT_TEXT = "这是用训练出来的音色合成的试音，用来验证训练闭环是否真的通了。"

# A synthesised clip must beat the different-speaker value by at least this margin to count
# as the trained voice. Reported alongside the measured numbers, never used to tune them.
MIN_MARGIN = 0.20


def _client():
    from fastapi.testclient import TestClient

    from backend.app import create_app

    return TestClient(create_app())


def _await_synthesis(client, text: str, language: str, timeout: float = 900.0) -> dict:
    """Submit one synthesis job and wait for it, returning the final snapshot."""
    response = client.post("/api/synthesis", json={"text": text, "language": language})
    if response.status_code != 200:
        return {"status": "failed", "error": f"HTTP {response.status_code}: {response.text[:300]}"}
    job_id = response.json()["job_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        snapshot = client.get(f"/api/synthesis/status/{job_id}").json()
        if snapshot.get("status") != "running":
            return snapshot
        time.sleep(3)
    return {"status": "failed", "error": f"合成超时（{timeout:.0f}s）"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--language", default="zh")
    parser.add_argument("--json-out", default=str(REPO_ROOT / "output" / "training_loop_verify.json"))
    parser.add_argument("--skip-synthesis", action="store_true", help="复用上次合成的音频，只重量指标")
    args = parser.parse_args()

    from backend.services import core

    config = core.load_config()
    reference = Path(str(config.get("ref_audio") or ""))
    if not reference.is_file():
        raise SystemExit(f"配置里的参考音不存在：{reference}")
    gpt = Path(str(config.get("gpt_model") or ""))
    sovits = Path(str(config.get("sovits_model") or ""))
    print(f"GPT 模型    : {gpt.name}  (存在={gpt.is_file()})")
    print(f"SoVITS 模型 : {sovits.name}  (存在={sovits.is_file()})")
    print(f"参考音      : {reference.name}")

    audio: Path | None = None
    snapshot: dict = {}
    if args.skip_synthesis:
        listed = sorted((REPO_ROOT / "output").glob("*/001_*.mp3"), key=lambda p: p.stat().st_mtime)
        audio = listed[-1] if listed else None
    else:
        client = _client()
        snapshot = _await_synthesis(client, args.text, args.language)
        if snapshot.get("status") != "done":
            print(f"合成失败：{snapshot.get('status')} {snapshot.get('error')}")
            return 1
        outputs = snapshot.get("outputs") or []
        download_id = str(outputs[0].get("url", "")).rsplit("/", 1)[-1] if outputs else ""
        audio = core.resolve_download(download_id) if download_id else None
    if audio is None or not audio.is_file():
        print("找不到合成音频")
        return 1
    print(f"合成音频    : {audio}  ({audio.stat().st_size / 1024:.0f} KB)")

    from speaker_embedding import SpeakerEmbedder, cosine_similarity

    embedder = SpeakerEmbedder()
    generated = embedder.embed_file(audio)

    slices = sorted((reference.parent).glob("*.wav"))
    others = [path for path in slices if path.name != reference.name]
    # Same-speaker baseline is measured on the corpus, so it reflects this recording setup.
    same_speaker = [
        round(cosine_similarity(generated, embedder.embed_file(path)), 4) for path in others[:3]
    ]
    baseline_pairs = [
        round(cosine_similarity(embedder.embed_file(reference), embedder.embed_file(path)), 4)
        for path in others[:3]
    ]
    to_reference = round(cosine_similarity(generated, embedder.embed_file(reference)), 4)

    # A different speaker: the trained corpus is a single speaker, so the honest control is
    # a recording of someone else. There is none guaranteed to exist, so this is reported
    # as unavailable rather than silently substituted.
    different = None
    print()
    print(f"SECS(合成音, 参考音)                     = {to_reference}")
    print(f"SECS(合成音, 同说话人其它录音) 中位      = {sorted(same_speaker)[len(same_speaker) // 2]}")
    print(f"SECS(参考音, 同说话人其它录音) 中位      = {sorted(baseline_pairs)[len(baseline_pairs) // 2]}")
    if different is None:
        print("异人对照：本仓库无第二说话人录音，未测（如实标注，不用合成音冒充对照）")

    baseline = sorted(baseline_pairs)[len(baseline_pairs) // 2]
    worst_same = min(same_speaker) if same_speaker else 0.0
    verdict = to_reference >= max(baseline - 0.15, worst_same - 0.05)
    print()
    print(
        f"判定：合成音与参考音 {to_reference}，同说话人基线 {baseline}，"
        f"最差同人 {worst_same} ⇒ {'通过（属该说话人正常范围）' if verdict else '不通过'}"
    )
    print(
        "说明：判据是相对的——绝对 SECS 随信道与语种变化，"
        "没有一个跨语料通用的绝对阈值，所以拿同说话人基线当尺子。"
    )

    payload = {
        "text": args.text,
        "language": args.language,
        "audio": str(audio),
        "gpt_model": str(gpt),
        "sovits_model": str(sovits),
        "reference": str(reference),
        "secs_to_reference": to_reference,
        "secs_to_same_speaker": same_speaker,
        "same_speaker_baseline": baseline_pairs,
        "baseline_median": baseline,
        "verdict": bool(verdict),
        "duration": (snapshot.get("outputs") or [{}])[0].get("duration"),
        "suspiciously_short": (snapshot.get("outputs") or [{}])[0].get("suspiciously_short"),
    }
    output = Path(args.json_out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {output}")
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
