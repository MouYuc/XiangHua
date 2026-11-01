"""
广普本地配音工具（基于 GPT-SoVITS v2Pro）

用法：
    python local_tts.py 台词.txt [输出目录]

台词文件：UTF-8，一行一句（建议一句 5~20 字）。
输出：每句一个 mp3，命名 001_台词前几字.mp3。

路径配置：全部通过环境变量覆盖（默认在本仓库下自动发现可用环境）：
    GPT_SOVITS_ROOT    GPT-SoVITS 仓库根目录（默认扫描项目根目录）
    GPT_MODEL          GPT 模型路径
    SOVITS_MODEL       SoVITS 模型路径
    REF_AUDIO          参考音频路径
    REF_TEXT           参考音频对应文本
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services.core import SynthesisBackend
from backend.services.pronunciation import build_pronunciation_plan
from tools.env import resolve_default_environment_root
from tools.rules import safe_name

GPT_SOVITS_ROOT = os.path.abspath(resolve_default_environment_root(Path(__file__).resolve().parents[1]))

import soundfile as sf

# ================= 配置区（可用环境变量覆盖） =================
GPT_MODEL = os.environ.get("GPT_MODEL", os.path.join(GPT_SOVITS_ROOT, "GPT_SoVITS", "pretrained_models", "s1v3.ckpt"))
SOVITS_MODEL = os.environ.get(
    "SOVITS_MODEL", os.path.join(GPT_SOVITS_ROOT, "GPT_SoVITS", "pretrained_models", "v2Pro", "s2Gv2Pro.pth")
)
REF_AUDIO = os.environ.get("REF_AUDIO", os.path.join(GPT_SOVITS_ROOT, "custom_data", "sliced", "训练.wav_0000018880_0000143680.wav"))
REF_TEXT = os.environ.get("REF_TEXT", "今天天气真不错，我们出去走走吧。")

TOP_K = int(os.environ.get("TOP_K", "30"))
TOP_P = float(os.environ.get("TOP_P", "0.88"))
TEMPERATURE = float(os.environ.get("TEMPERATURE", "0.95"))
SAMPLE_STEPS = int(os.environ.get("SAMPLE_STEPS", "64"))
SPEED = float(os.environ.get("SPEED", "1.0"))
PAUSE_SECOND = float(os.environ.get("PAUSE_SECOND", "0.15"))

# 口音档位：off / light / medium / heavy。旧 AUTO_LIGHTMID=1 映射为 medium。
ACCENT_LEVEL = os.environ.get("ACCENT_LEVEL", "off")
if os.environ.get("AUTO_LIGHTMID", "0") == "1" and "ACCENT_LEVEL" not in os.environ:
    ACCENT_LEVEL = "medium"
# =============================================================


def synth_line(text):
    return _BACKEND.synth_one(_config(), text)


def _config():
    return {
        "gpt_sovits_root": GPT_SOVITS_ROOT,
        "gpt_model": GPT_MODEL,
        "sovits_model": SOVITS_MODEL,
        "ref_audio": REF_AUDIO,
        "ref_text": REF_TEXT,
        "top_k": TOP_K,
        "top_p": TOP_P,
        "temperature": TEMPERATURE,
        "speed": SPEED,
        "pause_second": PAUSE_SECOND,
        "sample_steps": SAMPLE_STEPS,
    }


_BACKEND = SynthesisBackend()


def main():
    if len(sys.argv) < 2:
        print("用法: python local_tts.py 台词.txt [输出目录]")
        return
    src = sys.argv[1]
    if not os.path.exists(src):
        print("找不到文件:", src)
        return
    outdir = sys.argv[2] if len(sys.argv) > 2 else os.path.dirname(os.path.abspath(src))
    os.makedirs(outdir, exist_ok=True)

    with open(src, encoding="utf-8-sig") as f:
        lines = [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]
    if not lines:
        print("台词文件为空")
        return

    _BACKEND.ensure(_config())

    print(f"共 {len(lines)} 句，开始合成（每句约 10~30 秒）...")
    ok = 0
    for i, line in enumerate(lines, 1):
        text = build_pronunciation_plan(line, language="zh", accent_level=ACCENT_LEVEL).rendered_text
        out_wav = os.path.join(outdir, f"{i:03d}_{safe_name(line)}.wav")
        out_mp3 = out_wav[:-4] + ".mp3"
        try:
            audio, sr = synth_line(text)
            sf.write(out_wav, audio, sr)
            subprocess.run(
                ["ffmpeg", "-y", "-i", out_wav, "-b:a", "192k", out_mp3],
                capture_output=True,
                check=True,
            )
            os.remove(out_wav)
            ok += 1
            print(f"  OK {os.path.basename(out_mp3)}  [{text[:20]}]")
        except Exception as exc:
            print(f"  FAIL 第 {i} 句: {exc}")
    print(f"完成：{ok}/{len(lines)} 句成功 -> {outdir}")


if __name__ == "__main__":
    main()
