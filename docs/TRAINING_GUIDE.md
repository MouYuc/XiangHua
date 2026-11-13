# 训练流程详解 · Training Guide

> **2026-10-17 架构调整**：本文描述当前 GPT-SoVITS 基线训练管线，不是最终方案。新架构要求音色/发音/韵律/口音四分离；目标音频只用于音色，发音由字典决定。用户**不需要照读台词本，也不需要补录特定口音**；一段已有的自然说话录音即可。详见 [ARCHITECTURE.md](ARCHITECTURE.md)。

目标：用一段正常、授权的自然说话录音（建议 5–15 分钟，已有素材即可），训练出专属角色声线（GPT-SoVITS v2Pro 基线管线）。
本文档与 README「快速开始」配套，每步按 **目的 → 操作 → 结果** 组织。

> **有两种等价方式**：
> - **方式 A（推荐，无需命令行）**：用本仓库 WebUI 的「训练」页，五步全部在浏览器完成。
> - **方式 B（命令行）**：本页下面 0~6 步的原始命令，与方式 A 完全等价，适合习惯终端或想精细调参的用户。

## 方式 A · WebUI「训练」页（推荐，不依赖命令行）

启动 WebUI 后点侧边栏「**训练**」，从上到下依次操作；每步有状态徽章（待办 → 进行中 → 已就绪 / 出错），底部日志区实时显示任务输出（每 2 秒自动刷新，训练期间可关页面稍后回来看）：

1. **① 自然音频与切片**：上传一段已有自然说话录音（mp3/wav/m4a/flac 均可，自动放入 `custom_data/raw/`，多次上传会累加）→ 点「开始切片」。不需要照读台词本，也不需要为了训练重新配音。
2. **② Whisper 识别**：选模型（`large-v3` 最准，可换 `medium`/`small` 提速）→ 点「开始识别」。
3. **③ 可选校对**：系统会自动完成 Whisper 识别；点「加载识别结果」后，只在发现明显错字时修正（可试听切片）→ 点「保存校对」。没有发现问题可直接跳过，不要求用户逐字转写。
4. **④ 特征提取**：填实验名（音色名，如 `myvoice`，全程保持一致）→ 点「开始特征提取」（自动跑 1-get-text / 2-get-hubert / 2-get-sv / 3-get-semantic 四个子步，约 5~15 分钟）。若页面提示补丁未应用，先点「一键打广普补丁」。
5. **⑤ 训练**：确认 s1/s2 轮数（默认 15/12）→ 点「开始训练」（自动生成配置 → s1 韵律 → s2 音色，需数小时）。完成后点「**扫描训练产物 → 填入设置**」，模型路径自动写进默认配置，回「合成」页即可试音。

页面顶部有训练环境检查（根目录 / 补丁 / CUDA / faster-whisper / 切片数据），缺什么一目了然。

> 方式 B 的全部命令细节见下方 0~6 步；WebUI 内部调用的就是同一批脚本与参数，两种方式产物完全一致、可混用（例如命令行做过切片，WebUI 直接从第 ② 步继续）。

---

## 方式 B · 命令行逐步执行

> 目录约定：**仓库根目录** = 你运行 `python tools/webui.py` 的目录；
> **GPT-SoVITS 目录** = `$env:GPT_SOVITS_ROOT`（Windows）/ `$GPT_SOVITS_ROOT`（Linux·macOS）指向的目录；
> 默认扫描项目根目录下的一级子目录。

## 0. 环境要求

- Windows：Python 3.10 + PyTorch CUDA（2.6 稳定；2.13 在 Windows 有 gloo 崩溃问题，见「常见坑」）
- ffmpeg 需在 PATH（共享版）
- 8GB 显存可训练（batch 8 / 4）
- 把 `scripts/sitecustomize.py` 放进 `GPT_SoVITS/`（Windows torchaudio 修复）

## 第 1 步 · 准备自然说话音频

- **目的**：提供目标角色的自然音色素材
- **操作**：直接使用已有录音——访谈、播客、会议、语音备忘、已授权的配音素材等，放到 `GPT-SoVITS/custom_data/raw/`；也可以用手机自然说一段，但**不需要照读台词本**
- **注意**：安静环境、自然说话、不要播音腔；不需要为了“清晰/轻/中/浓口音”分别补录。口音由独立 accent pack 控制，不进入音色训练
- **授权**：只能使用本人、签约配音演员、版权方提供或明确许可的素材

## 第 2 步 · 切片 + 识别

- **目的**：把长录音拆成一句一句的短音频，再让电脑自动认出每句的文字

**Windows**：

```powershell
# ---------- ① 切片：长录音 → 一句一句的短音频 ----------
# 在 GPT-SoVITS 目录运行（即 $env:GPT_SOVITS_ROOT 指向的目录）
cd $env:GPT_SOVITS_ROOT
# 前 2 个参数是输入/输出目录，后面 9 个数字的含义见下方参数表
python -s tools/slice_audio.py custom_data/raw custom_data/sliced -35 4000 300 10 500 0.9 0.25 0 1

# ---------- ② 识别：短音频 → 每句文字 ----------
# 回到仓库根目录（你运行 WebUI 的目录），调用本仓库的识别脚本
cd <仓库根目录>
# 参数说明见下方「识别命令参数」表
python scripts\run_whisper.py --input ..\GPT-SoVITS\custom_data\sliced --output ..\GPT-SoVITS\custom_data\asr
```

**Linux / macOS**：

```bash
# ---------- ① 切片 ----------
cd "$GPT_SOVITS_ROOT"
python -s tools/slice_audio.py custom_data/raw custom_data/sliced -35 4000 300 10 500 0.9 0.25 0 1

# ---------- ② 识别 ----------
cd <仓库根目录>
python scripts/run_whisper.py --input ../GPT-SoVITS/custom_data/sliced --output ../GPT-SoVITS/custom_data/asr
```

**切片命令参数表**（`tools/slice_audio.py`，11 个参数全部列出，只调前 2 个即可，其余保持推荐值）：

| # | 参数 | 当前值 | 含义 |
|---|---|---|---|
| 1 | 输入目录 | `custom_data/raw` | 放录音的文件夹 |
| 2 | 输出目录 | `custom_data/sliced` | 切片保存位置 |
| 3 | 静音阈值 | `-35` | 音量低于该分贝（dB）视为静音、可在此处切割；`-40` 切得更碎，`-30` 更粗 |
| 4 | 最短段长 | `4000` | 每段最短时长（**毫秒**）；太短的段会自动与下一段合并，直到超过该值 |
| 5 | 最短间隔 | `300` | 两次切割之间的最小间隔（毫秒），避免切出碎段 |
| 6 | 检测步长 | `10` | 音量曲线计算步长（毫秒）；越小越精细但越慢，10 为推荐值 |
| 7 | 保留静音 | `500` | 段尾最多保留的静音长度（毫秒），即句间停顿约半秒 |
| 8 | 峰值上限 | `0.9` | 每段音量归一化的峰值上限（防止爆音） |
| 9 | 混合比例 | `0.25` | 音量拉平强度：`1` 完全拉平音量，`0` 完全保留原始响度；`0.25` 轻度平衡 |
| 10 | 分片编号 | `0` | 并行处理时的分片编号（从 0 开始）；单机跑保持 `0` |
| 11 | 分片总数 | `1` | 并行处理时的总分片数；`1` 表示不并行、全部处理 |

> 参数约束：最短段长 ≥ 最短间隔 ≥ 检测步长（4000 ≥ 300 ≥ 10 ✓），保留静音 ≥ 检测步长（500 ≥ 10 ✓）；改参数时别破坏这两个条件。

**识别命令参数表**（`scripts/run_whisper.py`）：

| 参数 | 当前值 | 含义 |
|---|---|---|
| `--input` | 切片目录 | 第 ① 步产出的切片 wav 目录（必填） |
| `--output` | 标注目录 | 标注输出位置（必填），生成 `sliced_whisper.list` |
| `--model` | `large-v3`（默认） | faster-whisper 模型；`large-v3` 最准但慢、吃显存，可换 `small` / `medium` 提速 |
| `--language` | `zh`（默认） | 识别语言；中文保持默认 |

- **完成后你会看到**：`GPT-SoVITS/custom_data/asr/sliced_whisper.list`，每行一句，格式 `音频路径|sliced|ZH|识别文字`

## 第 3 步 · 可选校对

- **目的**：电脑对广东口音常认错字；改对后模型才不会学错
- **操作**：系统已自动生成 `sliced_whisper.list`。只有发现明显错字时，才需要打开文件修正；没有时间可先跳过，不能把逐字校对作为默认前置负担
- **句子很多时**：先编辑 `scripts/fix_transcript.py` 里的修正表（按行号填正确文本），再批量替换：

```bash
# 在仓库根目录运行：用修正表批量替换识别文本
# 参数：--input 要修正的 .list、--output 修正后的 .list；替换内容在脚本开头的 corrections 字典里按行号填写
python scripts/fix_transcript.py --input ../GPT-SoVITS/custom_data/asr/sliced_whisper.list --output ../GPT-SoVITS/custom_data/asr/sliced_corrected.list
```

- **完成后你会看到**：每行文字与录音一致；WebUI「训练」页顶部的「标注行数」会显示该文件行数

## 第 4 步 · 特征提取

- **目的**：把「文字 + 你的声音」转成模型能学的特征
- **操作**：在 GPT-SoVITS 目录，先设置环境变量，再依次运行 4 个脚本（Windows 用 `set`，Linux/macOS 用 `export`；下面以 Windows 为例）

```powershell
# ---------- 特征提取（在 GPT-SoVITS 目录运行，Windows 示例） ----------
set inp_text=custom_data\asr\sliced_corrected.list   # 校对后的标注文件
set inp_wav_dir=custom_data\sliced                   # 切片音频目录
set exp_name=myvoice                                 # 实验名（你的音色名，可自定义）
set i_part=0 & set all_parts=1
set opt_dir=custom_data\dataset                      # 特征输出目录
set bert_pretrained_dir=GPT_SoVITS\pretrained_models\chinese-roberta-wwm-ext-large
set version=v2Pro

# ① 文字 → 音素 + BERT 文本特征（必须在校对之后）
python -s GPT_SoVITS/prepare_datasets/1-get-text.py

set cnhubert_base_dir=GPT_SoVITS\pretrained_models\chinese-hubert-base
# ② 音频 → HuBERT 音频特征
python -s GPT_SoVITS/prepare_datasets/2-get-hubert-wav32k.py

set sv_path=GPT_SoVITS\pretrained_models\sv\pretrained_eres2netv2w24s4ep4.ckpt
# ③ 音频 → 声纹特征
python -s GPT_SoVITS/prepare_datasets/2-get-sv.py

set pretrained_s2G=GPT_SoVITS\pretrained_models\v2Pro\s2Gv2Pro.pth
set s2config_path=GPT_SoVITS\configs\s2v2Pro.json
# ④ 音频 → 语义 token
python -s GPT_SoVITS/prepare_datasets/3-get-semantic.py
```

> ⚠️ **必须先打 `patches/pinyin.patch` 再做 1-get-text**，粤字读音才会进音素标注。

## 第 5 步 · 训练

- **目的**：把特征训练成你的专属模型（全流程最耗时，8GB 显存可跑）
- **操作**：先生成训练配置（在仓库根目录），再开始训练（在 GPT-SoVITS 目录）

**① 生成配置（在仓库根目录）**：

```powershell
# 生成 s1/s2 训练配置，输出到 ../GPT-SoVITS/TEMP/
# 参数：--dataset 数据集目录（第 4 步产物） --exp 实验名（与第 4 步一致）
#       --s1-epochs 韵律模型训练轮数（默认 15） --s2-epochs 音色模型训练轮数（默认 12）
python scripts\build_train_configs.py --dataset ..\GPT-SoVITS\custom_data\dataset --exp myvoice
```

```bash
# Linux / macOS
python scripts/build_train_configs.py --dataset ../GPT-SoVITS/custom_data/dataset --exp myvoice
```

**② 开始训练（在 GPT-SoVITS 目录）**：

```powershell
cd $env:GPT_SOVITS_ROOT
# s2 的保存目录需要先手动创建（脚本不会自动建）
mkdir custom_data\dataset\logs_s2_v2Pro

# ① 训练韵律模型 s1（GPT：文本 → 语义），约 15 epoch
python -s GPT_SoVITS\s1_train.py --config_file TEMP\tmp_s1.yaml
# ② 训练音色模型 s2（SoVITS：语义 → 声波），约 12 epoch
python -s GPT_SoVITS\s2_train.py --config TEMP\tmp_s2.json
```

```bash
# Linux / macOS
cd "$GPT_SOVITS_ROOT"
mkdir -p custom_data/dataset/logs_s2_v2Pro
python -s GPT_SoVITS/s1_train.py --config_file TEMP/tmp_s1.yaml
python -s GPT_SoVITS/s2_train.py --config TEMP/tmp_s2.json
```

- **完成后你会看到**：
  - `GPT_weights_v2Pro/myvoice-e*.ckpt`（s1 产物）
  - `SoVITS_weights_v2Pro/myvoice_e*.pth`（s2 产物）

## 第 6 步 · 合成验收

- **目的**：用你的新模型念一段台词，检查效果
- **操作**（在仓库根目录，模型路径换成第 5 步的产物；GPT-SoVITS 平级时用 `../GPT-SoVITS` 前缀）：

```bash
# 用环境变量指定模型路径（Windows 用 $env:GPT_MODEL=... 形式，路径同样换成你的产物）
GPT_MODEL=../GPT-SoVITS/GPT_weights_v2Pro/myvoice-e14.ckpt \
SOVITS_MODEL=../GPT-SoVITS/SoVITS_weights_v2Pro/myvoice_e12_s396.pth \
python scripts/local_tts.py 台词.txt out/
```

- **不满意先调参数**：卡顿/噪点 → 确认 `SAMPLE_STEPS` ≥ 64、换更干净的参考片段；语气生硬 → 微调 `TEMPERATURE`（默认 0.95）；都试过仍不行，再补充更多**已有自然说话录音**，不要求照台词本补录

## 常见坑

- **torch 2.13 Windows 崩溃**（gloo all_reduce 访问违例）：降到 torch 2.6
  `pip install torch==2.6.0 torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/cu126`
- **torchcodec DLL 加载失败**：卸载 torchcodec + 用 sitecustomize.py（soundfile 后端）
- **补丁后粤字仍读普通话**：补丁必须**在训练前**生效（重新 1-get-text）
- **s2 训练保存路径不存在报错**：先 `mkdir logs_s2_v2Pro`
- **合成卡顿/噪点**：`SAMPLE_STEPS=64`、换干净参考片段；根治靠更多干净、已授权的自然录音，而不是让用户补录台词
