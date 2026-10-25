# 使用手册 · Usage Guide

README 的扩展版：一键安装、命令行批量合成与 FAQ。

> **2026-10-17 状态**：当前默认是“清晰优先”（底模 v2Pro + 清晰参考音），默认 `accent_level=off`。音色入口已改为**一段自然说话音频**：系统自动切片、识别、选参考片段，不需要照读台词本或补录口音。口音为 `off / light / medium / heavy` 四档，固定搭配受保护。首轮验证见 [MVP_VALIDATION.md](MVP_VALIDATION.md)，新架构见 [ARCHITECTURE.md](ARCHITECTURE.md)。

本仓库**不携带 GPT-SoVITS 环境**（它是独立的 MIT 项目，本体 + 模型约 15GB）。
`setup.ps1` / `setup.sh` 会自动完成 6 步：

1. 克隆 GPT-SoVITS 并**锁定到补丁对应的 commit**（`d523079`），避免上游更新导致补丁失效
2. 创建 Python 3.10 虚拟环境（`.venv`，有 `uv` 时自动用 uv）
3. 安装 PyTorch 2.6 + CUDA（Windows 用 CUDA 12.6，Linux 用 12.1，macOS 自动 CPU 版）
4. 安装 GPT-SoVITS 依赖（含 faster-whisper、noisereduce）
5. 应用广普发音补丁 + （Windows）torchaudio 音频修复
6. 下载预训练底模（默认 ModelScope 源，可换 HuggingFace 镜像）

```powershell
# Windows
.\setup.ps1
.\setup.ps1 -Source HF-Mirror                 # 换 HuggingFace 镜像
.\setup.ps1 -GPT_SOVITS_ROOT D:\GPT-SoVITS    # 指定 GPT-SoVITS 位置
```

```bash
# Linux / macOS
bash setup.sh
bash setup.sh HF-Mirror
GPT_SOVITS_ROOT=/path/to bash setup.sh
```

> 💡 脚本锁定的 GPT-SoVITS 版本为 `d523079`；手动使用新版 GPT-SoVITS 时补丁可能需要重新生成，见 [patches/README.md](../patches/README.md)。

## 命令行批量合成

**前提**：已安装 GPT-SoVITS，并配置好自己的/已授权的角色声线。

```bash
cd GPT-SoVITS
git apply ../XiangHua/patches/pinyin.patch          # 打广普补丁（训练前做一次）
cp ../XiangHua/scripts/sitecustomize.py GPT_SoVITS/         # Windows 音频修复（可选）
python ../XiangHua/tools/local_tts.py 台词.txt 输出目录
```

**台词文件**：UTF-8 编码，一行一句（建议 5~20 字），`#` 开头为注释。写普通话即可；默认不做口音替换，需要时用 `ACCENT_LEVEL` 或 WebUI 的“口音档位”控制。

**环境变量**（默认在项目根目录内自动发现可用环境）：

| 变量 | 作用 | 默认值 |
|---|---|---|
| `GPT_SOVITS_ROOT` | GPT-SoVITS 根目录 | 扫描项目根目录一级子目录 |
| `GPT_MODEL` / `SOVITS_MODEL` | 模型路径 | 当前配置（默认清晰优先底模） |
| `REF_AUDIO` / `REF_TEXT` | 参考音频与文本；WebUI 上传会自动生成，CLI 可手动覆盖 | `custom_data/sliced/训练.wav...` |
| `TOP_K` / `TOP_P` / `TEMPERATURE` | 采样参数 | 30 / 0.88 / 0.95 |
| `SAMPLE_STEPS` | 扩散步数（卡顿就调高） | 64 |
| `SPEED` / `PAUSE_SECOND` | 语速 / 句间停顿 | 1.0 / 0.15 |
| `ACCENT_LEVEL` | 口音档位：`off / light / medium / heavy` | `off` |
| `AUTO_LIGHTMID` | 旧兼容开关；设为 `1` 时映射为 `medium` | `0` |

```bash
# 用环境变量覆盖模型路径的示例
GPT_MODEL=/path/to/model.ckpt SOVITS_MODEL=/path/to/model.pth \
python tools/local_tts.py 台词.txt out/
```

## 20 句回归与客观验收

固定样本在 `backend/tests/fixtures/tts_regression_20.json`，覆盖清晰普通话、技术句、轻/中/浓广普。完整运行会输出音频、CER、SECS、时长、削波和异常静音：

```powershell
.\.venv\Scripts\python.exe scripts\tts_regression.py
```

复用已生成音频，只重算指标：

```powershell
.\.venv\Scripts\python.exe scripts\tts_regression.py --skip-synthesis
```

报告在 `output/tts_regression/report.json`。CER 是可懂度护栏，SECS 是音色相似度；两者必须一起看，不能只看 SECS。

同文本、只改口音档位的交换测试：

```powershell
.\.venv\Scripts\python.exe scripts\accent_swap_verify.py
```

## WebUI

浏览器图形界面，六个页面：**合成 / 批量合成 / 环境检测 / 训练 / 历史记录 / 设置**。
「训练」页支持从一段自然说话音频到专属音色的全流程（自动切片、ASR、可选校对、特征提取、s1+s2 训练），全部在浏览器完成、无需命令行；不需要照读台词本；
训练结束后「扫描训练产物 → 填入设置」即可回「合成」页用新音色。环境检测页会直接告诉你缺什么依赖并给出修复命令。

页面详解、配置优先级、WebUI 专属 FAQ 见 [WEBUI_GUIDE.md](WEBUI_GUIDE.md)。

> Docker 部署说明已在 2026-10-17 清场中移除；当前只支持本地运行。

## 常见问题

**Q：合成声音有噪点 / 卡顿？**
小数据训练常见。先试 `SAMPLE_STEPS=64`、换更干净的参考片段；根治靠更多干净、已授权的自然说话录音，而不是让用户照稿补录。

**Q：粤字还是按普通话念？**
补丁没生效：确认 `git apply` 成功，且**训练前**重新执行了 1-get-text.py（补丁只影响训练阶段的音素标注）。

**Q：Windows 上 torchaudio / torchcodec 报错？**
用 `scripts/sitecustomize.py`（soundfile 后端）；torch 2.13 在 Windows 有 gloo 崩溃问题，建议降到 2.6。

**Q：WebUI 启动很慢？**
启动时预加载模型需 10~30 秒，属正常；启动后合成立即开始。

**Q：试听没声音 / 下载不了？**
输出目录保持在仓库 `output/` 内（默认如此），否则浏览器无法访问。

**Q：端口被占用？**
设置 `XIANGHUA_SERVER_PORT=7861` 后重启。

## 目录结构

```text
XiangHua/
├── backend/             # FastAPI 合成、训练、音色服务
├── frontend/            # Vue 3 WebUI
├── tools/               # local_tts / webui / train
├── scripts/             # 训练链、数据准备、验收
├── GPT-SoVITS/          # GPT-SoVITS v2Pro 引擎与模型
├── assets/              # 口音包清单、Logo（旧台词本仅作可选示例）
├── docs/                # 架构、产品、训练与使用文档
├── examples/            # 示例台词
└── patches/             # 发音补丁
```