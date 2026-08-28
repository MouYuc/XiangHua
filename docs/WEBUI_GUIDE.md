# WebUI 使用说明

本地图形界面，打开浏览器即可完成：写台词 → 合成 → 试听 → 下载。
页面：**合成 / 批量合成 / 环境检测 / 训练 / 历史记录 / 设置**。

## 启动

```powershell
# Windows
$env:GPT_SOVITS_ROOT = 'D:\AIVoice\GPT-SoVITS'   # 换成你的 GPT-SoVITS 路径
python tools\webui.py
```

```bash
# Linux / macOS
export GPT_SOVITS_ROOT=/path/to/GPT-SoVITS
python tools/webui.py
```

浏览器打开 <http://127.0.0.1:7860>。

### Docker

在项目根目录准备 `models/pretrained_models`、`models/G2PWModel`、`my_voice/custom_data`、
`my_voice/GPT_weights_v2Pro`、`my_voice/SoVITS_weights_v2Pro`、
`.guangpu_webui` 和 `output` 后执行：

```bash
docker compose up -d
```

容器 WebUI 监听 `0.0.0.0:7860`，主机通过 <http://127.0.0.1:7860> 访问。
容器项目根目录固定为 `/workspace`，环境检测扫描其直接子目录；默认的
`/workspace/GPT-SoVITS` 会自动被识别。额外挂载的环境也必须直接位于
`/workspace/<环境名>`，并包含 `GPT_SoVITS` 子目录。

> 如果 `GPT_SOVITS_ROOT` 不设置，默认扫描项目根目录下的一级子目录，选择包含 `GPT_SoVITS` 的环境。
> 也可以在 WebUI 的「设置」页填写路径并保存，写入 `.guangpu_webui/config.json`（不会入库）。

## 页面说明

- **合成**：多行台词（一行一句）+ 文本语言选择 + 自动转轻中广普开关 + 六个参数滑块（默认 H4 定稿配方）+ 模型/参考音频（可上传或改路径）+ 逐句输出（试听 + 下载）。
- **批量合成**：上传 `.txt` 台词文件，整批合成，完成后打包 zip 下载。
- **环境检测**：检查 Python / PyTorch+CUDA / 依赖包 / ffmpeg / GPT-SoVITS 根目录 / 广普补丁 / 模型 / 参考音频，缺什么直接给修复命令。
- **训练**：从录音到专属音色的完整流程，全部在浏览器完成、无需命令行：
  1. **录音切片**：上传录音（10~15 分钟），自动切成一句一句
  2. **Whisper 识别**：自动给每句切片打文字
  3. **人工校对**：表格逐句核对/修改识别文本（广普错字多，关键一步），可试听任意切片
  4. **特征提取**：一键跑 4 个子步（BERT / HuBERT / 声纹 / 语义）
  5. **训练**：自动生成配置 → s1（韵律）→ s2（音色），支持后台运行、实时日志、随时停止；完成后「扫描训练产物 → 填入设置」即可回「合成」页用新音色。
  页面顶部另有训练环境检查与「一键打广普补丁」。
- **历史记录**：最近 200 条合成记录，可回听、下载、清空。
- **设置**：GPT-SoVITS 根目录与模型/参考音频路径，保存为本机默认。

### 多语言合成与参考音频

1. 在“模型与参考音频”中上传本地音频，或直接填写已有参考音频路径；上传支持 wav、mp3、m4a、flac 和 ogg，单个文件最大 100 MB。
2. 选择“参考语言”，并填写与参考音频内容对应的参考文本。
3. 在台词区域选择“文本语言”。文本语言和参考语言均支持中文、粤语、英语、日语、韩语，二者可以不同。
4. 输入台词后开始合成。例如选择“中文参考语言”和“英语文本语言”，即可用中文参考音色尝试朗读英文台词。
5. 点击“保存为默认”后，参考音频路径、参考文本和两项语言选择会在下次启动时恢复。

“自动转轻中广普”只在文本语言为中文时生效；选择粤语、英语、日语或韩语时，原文会直接传入模型。

### 发音补丁导入

Docker 镜像不再内置发音补丁。请从仓库获取 `patches/pinyin.patch`，然后在 WebUI 的
“模型与参考音频 → 发音补丁”中导入。导入器会先执行 `git apply --check`，校验通过后才
应用到当前 GPT-SoVITS 环境；不兼容、已应用或超过 1 MB 的文件会被拒绝。

### 实时批量合成

合成页的“导入 TXT + 批量合成”和“开始合成”共用同一后台任务和 SSE 流。每条音频完成后，
该输出对应的交织粒子份额会立即聚合；播放任一结果时，粒子球只响应这条音频的 token 流。

## 配置优先级

内置默认值 < 环境变量 < `.guangpu_webui/config.json` < 界面手动修改（当次会话）。

常用环境变量：

| 变量 | 作用 |
|---|---|
| `GPT_SOVITS_ROOT` | GPT-SoVITS 根目录 |
| `GPT_MODEL` / `SOVITS_MODEL` | 模型路径 |
| `REF_AUDIO` / `REF_TEXT` | 参考音频与文本 |
| `TOP_K` / `TOP_P` / `TEMPERATURE` / `SAMPLE_STEPS` / `SPEED` / `PAUSE_SECOND` | 合成参数 |
| `AUTO_LIGHTMID` | `0` 关闭自动转广普 |
| `XIANGHUA_SERVER_NAME` / `XIANGHUA_SERVER_PORT` | 监听地址 / 端口（本机默认 127.0.0.1:7860，Docker Compose 默认 0.0.0.0:7860） |

## 常见问题

**启动很慢？**
启动时会预加载模型，需要 10~30 秒，属于正常现象；启动后合成立即开始，无需再等模型加载。

**试听按钮没有声音 / 文件下载不了？**
输出目录建议保持在仓库 `output/` 内（默认如此）。自定义目录请放在仓库内，否则浏览器无法访问。

**ffmpeg 缺失？**
合成会降级输出 wav；环境检测页会给出安装命令（Windows：`winget install Gyan.FFmpeg`）。

**粤字还是按普通话念？**
说明广普补丁未应用：在 GPT-SoVITS 里执行 `git apply patches/pinyin.patch`，并重新跑训练的特征提取；已训练模型不受影响。

**端口被占用？**
设置 `XIANGHUA_SERVER_PORT=7861` 后重启。

## 安全说明

- WebUI 默认只监听本机回环地址（127.0.0.1），不对外网开放。
- 本仓库与 WebUI 不含任何人的声音克隆模型、录音或声纹特征；请使用自己的声音训练。
