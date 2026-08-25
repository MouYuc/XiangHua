# 使用手册 · Usage Guide

README 的扩展版：一键安装、命令行批量合成、Docker 部署与 FAQ 的完整说明。

## 一键安装

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

**前提**：已安装 GPT-SoVITS，且已训练好你自己的广普音色模型。

```bash
cd GPT-SoVITS
git apply ../XiangHua/patches/pinyin.patch          # 打广普补丁（训练前做一次）
cp ../XiangHua/scripts/sitecustomize.py GPT_SoVITS/         # Windows 音频修复（可选）
python ../XiangHua/tools/local_tts.py 台词.txt 输出目录
```

**台词文件**：UTF-8 编码，一行一句（建议 5~20 字），`#` 开头为注释。写普通普通话即可，工具自动转「轻中」广普（是→系、不是→唔系、最重要→最紧要、没有→冇、什么→乜嘢…）。

**环境变量**（默认在项目根目录内自动发现可用环境）：

| 变量 | 作用 | 默认值 |
|---|---|---|
| `GPT_SOVITS_ROOT` | GPT-SoVITS 根目录 | 扫描项目根目录一级子目录 |
| `GPT_MODEL` / `SOVITS_MODEL` | 模型路径 | `GPT_weights_v2Pro/guangpu07-e14.ckpt` 等 |
| `REF_AUDIO` / `REF_TEXT` | 参考音频与对应文本 | `custom_data/sliced/训练.wav...` |
| `TOP_K` / `TOP_P` / `TEMPERATURE` | 采样参数 | 30 / 0.88 / 0.95 |
| `SAMPLE_STEPS` | 扩散步数（卡顿就调高） | 64 |
| `SPEED` / `PAUSE_SECOND` | 语速 / 句间停顿 | 1.0 / 0.15 |
| `AUTO_LIGHTMID` | 设为 `0` 关闭自动转广普 | `1` |

```bash
# 用环境变量覆盖模型路径的示例
GPT_MODEL=/path/to/myvoice-e14.ckpt SOVITS_MODEL=/path/to/myvoice_e12.pth \
python tools/local_tts.py 台词.txt out/
```

## WebUI

浏览器图形界面，六个页面：**合成 / 批量合成 / 环境检测 / 训练 / 历史记录 / 设置**。
「训练」页支持从录音到专属音色的全流程（切片、识别、表格校对、特征提取、s1+s2 训练），全部在浏览器完成、无需命令行；
训练结束后「扫描训练产物 → 填入设置」即可回「合成」页用新音色。环境检测页会直接告诉你缺什么依赖并给出修复命令。

页面详解、配置优先级、WebUI 专属 FAQ 见 [WEBUI_GUIDE.md](WEBUI_GUIDE.md)。

## Docker 部署

推送 `main` / `v*` 后，[Actions 工作流](../.github/workflows/docker-image.yml) 自动构建并**双推**
[GHCR](https://github.com/MouYuc/Clone-youself/pkgs/container/clone-youself) 与
[Docker Hub](https://hub.docker.com/r/ZeroJy/clone-youself)（镜像不含任何声音模型）。

```bash
# 1. 准备目录（底模从 GPT-SoVITS 官方仓库下载）
mkdir -p models/pretrained_models models/G2PWModel my_voice/custom_data \
  my_voice/GPT_weights_v2Pro my_voice/SoVITS_weights_v2Pro .guangpu_webui output

# 2. 拉取镜像并启动与本机一致的 WebUI（需要 NVIDIA Container Toolkit）
docker compose up -d

# 3. 查看启动日志
docker compose logs -f XiangHua
```

浏览器打开 <http://127.0.0.1:7860>。Compose 会自动拉取
`ghcr.io/mouyuc/clone-youself:latest`，也可以先执行 `docker pull` 预拉取。
停止服务：`docker compose down`。

Compose 默认挂载以下目录：

- `models/pretrained_models`、`models/G2PWModel`：公开底模
- `my_voice/custom_data`：参考音频和训练数据，映射到 `/workspace/GPT-SoVITS/custom_data`
- `my_voice/GPT_weights_v2Pro`、`my_voice/SoVITS_weights_v2Pro`：个人训练模型，映射到 GPT-SoVITS 对应目录
- `.guangpu_webui`：WebUI 配置与历史记录
- `output`：合成结果，可在浏览器直接试听和下载

容器内项目根目录是 `/workspace`。环境检测只扫描它的直接子目录，并识别其中包含
`GPT_SoVITS` 的目录，因此内置的 `/workspace/GPT-SoVITS` 会自动出现；如需添加其他环境，
将其挂载为 `/workspace/<环境名>`，且该目录下必须有 `GPT_SoVITS` 子目录。

- 首次推送后约 **15~30 分钟**出镜像，可在仓库 **Actions** 页查看构建进度
- 想让镜像同时出现在 Docker Hub（Docker Pulls 徽章统计的就是它）：在仓库 **Settings → Secrets and variables → Actions** 配置 `DOCKERHUB_USERNAME` 与 `DOCKERHUB_TOKEN`（Docker Hub 账号 → Account Settings → Security → New Access Token），下次推送自动双推
- 训练自己的音色时，把录音放进 `my_voice/` 后直接在 WebUI「训练」页执行

## 常见问题

**Q：合成声音有噪点 / 卡顿？**
小数据训练常见。先试 `SAMPLE_STEPS=64`、换更干净的参考音频；根治靠加录音量（10 分钟以上）重训。

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
├── setup.ps1 / setup.sh        # 一键环境安装（Windows / Linux·macOS）
├── Dockerfile                  # 环境镜像（不含声音模型）
├── docker-compose.yml
├── .github/workflows/          # 推送 GitHub 后自动构建镜像 → GHCR + Docker Hub
├── patches/
│   ├── pinyin.patch    # 广普发音词典补丁（15 个粤字读音）
│   └── README.md               # 补丁说明 + 词典对照表
├── tools/
│   ├── local_tts.py    # 批量合成工具（可移植，环境变量配置）
│   ├── webui.py        # WebUI（浏览器操作：合成 + 训练页）
│   └── train.py        # 训练后端（切片/识别/特征/训练的后台任务封装）
├── scripts/
│   ├── run_whisper.py          # 切片语音识别
│   ├── fix_transcript.py       # 标注人工修正模板
│   ├── build_train_configs.py  # 训练配置生成
│   └── sitecustomize.py        # Windows torchaudio 修复
├── assets/
│   ├── logo.png                # 项目 Logo
│   └── 广普训练台词本.txt       # 录音参考台词
├── examples/                   # 两个可直接运行的台词示例（含 # 注释行支持）
└── docs/
    ├── USAGE_GUIDE.md          # 使用手册（本文档）
    ├── WEBUI_GUIDE.md          # WebUI 使用说明
    ├── TRAINING_GUIDE.md       # 训练流程详解
    └── BADGES_GUIDE.md         # 手动添加徽章教程
```
