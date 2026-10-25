# 媒体采集能力交接文档

> 更新时间：2026-10-17
> 交接范围：上传媒体、说话人分段、句级切片、质检、可选转写、训练集导出与训练工作区接入
> 当前状态：本地单机可用，已完成真实双说话人冒烟测试与 Playwright 页面验证

---

## 0. 一句话结论

这次交付把“上传一个视频/音频”变成了可复核的训练集生产线：

```text
上传媒体
  → ffmpeg 提取 16k/32k 音轨
  → VAD 检测语音
  → ERes2NetV2 提取说话人 embedding
  → 层次聚类说话人
  → 2–10 秒句级切片
  → DSP 质检
  → 可选 faster-whisper 转写
  → DatasetVersion（manifest + list + checksums）
  → 写入训练工作区
```

默认做的是“谁在何时说话”的说话人分段，不是音源分离。BGM、重叠说话和角色变声会影响准确率。

---

## 1. 当前可交付能力

### 1.1 上传

- 支持本地视频/音频：`mp4 / mkv / mov / webm / avi / wav / mp3 / m4a / flac / ogg / aac`
- 上传前必须勾选“我有权将该素材用于训练”
- 保存原始文件、SHA256、时长、编码、采样率、声道数
- 不做流媒体平台抓取

### 1.2 说话人分段

- VAD：25ms 帧 / 10ms hop，噪声底 + 峰值双阈值
- 2 秒窗口 / 1 秒 hop 提取 speaker embedding
- ERes2NetV2 层次聚类（cosine + average linkage）
- 可自动估计人数，也可手动指定 1–12 人
- 输出说话人卡片：试听样例、总时长、片段数、置信度
- 用户选择目标说话人后，只导出该说话人的片段

### 1.3 切片

- 默认 2–10 秒
- 长 turn 优先在低能量点切分，避免切断字词
- 前后保留 80ms
- 输出 32kHz 单声道 WAV
- 每段记录原始时间范围、校验和、QC 结果

### 1.4 质检

复用 `backend/services/reference_audio.py` 的确定性 DSP 门禁：

- 削波
- DC 偏移
- 电平
- 静音比例
- 有效带宽
- SNR 估计

QC `fail` 的片段默认不进入导出/训练集；UI 可勾选“包含质检失败片段”。

### 1.5 转写

- 可选 faster-whisper
- 优先使用本地模型目录，避免访问 HuggingFace
- 每段单独转写
- 失败时写入 `text_error`，不阻塞整个分析任务

### 1.6 训练集导出

生成：

```text
custom_data/media/<session_id>/dataset/<dataset_id>/
  dataset.list
  manifest.json
  checksums.sha256
```

manifest 记录来源、授权声明、说话人、统计、train/val 划分、被拒绝片段和 `train_ready`。

### 1.7 写入训练工作区

`/activate` 会：

1. 把目标说话人的切片复制到 `custom_data/sliced/media_<session_id>_<segment_id>.wav`
2. 写入 `custom_data/asr/sliced_corrected.list`
3. 转写就绪时同步写入 `custom_data/asr/sliced_whisper.list`
4. 备份旧清单到 `.bak-<timestamp>-<uuid>`
5. 把训练步骤 `slice / asr / correct` 标记为完成

之后回到“训练”页即可做特征提取。

---

## 2. 不包含 / 边界

- 不做流媒体平台爬取适配器
- 不把无授权动漫角色/配音演员声线做成克隆模型
- 默认不做音源分离（source separation）
- 不承诺对 BGM、重叠说话、角色变声做完美分轨
- 第一版一个采集会话只接收一个媒体文件
- 没有“手动合并/拆分片段”的 UI
- 没有远程演员邀请采集
- 没有多租户、配额、对象存储、License Ledger

---

## 3. 架构与数据流

```text
Frontend「采集」页
  └─ MediaIngestPanel.vue
       ├─ POST /api/media/sessions            上传 + 自动开始分析
       ├─ GET  /api/media/sessions            最近会话
       ├─ GET  /api/media/sessions/{id}       会话详情 + 轮询
       ├─ POST /api/media/sessions/{id}/analyze
       ├─ POST /api/media/sessions/{id}/select-speaker
       ├─ POST /api/media/sessions/{id}/export
       └─ POST /api/media/sessions/{id}/activate

Backend
  ├─ backend/app.py                         路由、URL 适配、错误处理
  ├─ backend/services/media_ingest.py        会话存储、ffmpeg、QC、ASR、导出、训练接入
  └─ backend/services/media_diarization.py   VAD、embedding 聚类、turn、切片边界
```

---

## 4. 文件地图

### 4.1 新增

| 文件 | 作用 |
|---|---|
| `backend/services/media_ingest.py` | 媒体会话、上传、音轨提取、分析编排、QC、ASR、数据集导出、训练工作区接入 |
| `backend/services/media_diarization.py` | VAD、窗口 embedding、层次聚类、turn 合并、长 turn 切分 |
| `backend/tests/test_media_ingest.py` | 15 项媒体采集测试 |
| `frontend/src/components/MediaIngestPanel.vue` | “采集”页 |
| `frontend/src/components/MediaIngestPanel.test.ts` | 采集页契约测试 |

### 4.2 修改

| 文件 | 改动 |
|---|---|
| `backend/app.py` | 新增 `/api/media/sessions` 系列路由和 `MediaIngestError` 处理 |
| `backend/schemas.py` | 新增 `MediaAnalyzeRequest`、`MediaSelectSpeakerRequest`、`MediaExportRequest` |
| `frontend/src/App.vue` | 新增 `media` 页面并挂载 `MediaIngestPanel` |
| `frontend/src/components/IconRail.vue` | 主导航新增“采集” |
| `frontend/src/components/IconRail.test.ts` | 导航契约更新 |
| `frontend/src/layout.test.ts` | Page 联合类型断言更新 |
| `frontend/src/components/TimbrePanel.test.ts` | Page 联合类型断言更新 |
| `docs/ARCHITECTURE.md` | 代码映射补充媒体采集模块 |
| `docs/PROJECT_MEMORY.md` | 记录媒体采集能力与验证状态 |

### 4.3 设计/计划文档

- `docs/superpowers/specs/2026-10-17-media-ingest-design.md`
- `docs/superpowers/plans/2026-10-17-media-ingest.md`

这两个目录在 `.gitignore` 中，不会进入 git。

---

## 5. 运行方式

### 5.1 启动 WebUI

```powershell
cd G:\Clone-youself
& .\.venv\Scripts\python.exe tools\webui.py
```

默认地址：

```text
http://127.0.0.1:7860
```

可用环境变量改端口：

```powershell
$env:XIANGHUA_SERVER_PORT = "7861"
& .\.venv\Scripts\python.exe tools\webui.py
```

### 5.2 前端开发

```powershell
cd G:\Clone-youself\frontend
npm test
npm run build
```

### 5.3 依赖检查

- `ffmpeg` / `ffprobe` 必须在 PATH 中
- ERes2NetV2 checkpoint：

```text
G:\Clone-youself\GPT-SoVITS\GPT_SoVITS\pretrained_models\sv\pretrained_eres2netv2w24s4ep4.ckpt
```

- faster-whisper 本地模型当前机器路径：

```text
G:\Clone-youself\.guangpu_webui\models\models\Systran--faster-whisper-large-v3\snapshots\master
```

模型查找逻辑在 `scripts/run_whisper.py::resolve_local_model()`。

---

## 6. 使用流程

1. 打开 WebUI，点击左侧“采集”。
2. 选择视频/音频。
3. 填名称、语言、说话人数（留空自动）。
4. 决定是否自动转写。
5. 勾选“我有权将该素材用于训练”。
6. 点击“上传并开始分析”。
7. 页面轮询任务状态，完成后显示说话人卡片。
8. 试听说话人样例，点击卡片选择目标说话人。
9. 在“切片与质检”表里试听、检查 QC 和文本。
10. 点击“导出训练集”生成 manifest / list / checksums。
11. 点击“写入训练工作区”。
12. 回到“训练”页，直接做特征提取。

---

## 7. API 参考

### 7.1 上传并创建会话

```http
POST /api/media/sessions
Content-Type: multipart/form-data
```

表单字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `recording` | file | 是 | 视频/音频文件 |
| `name` | string | 否 | 会话名称，默认文件名 |
| `language` | string | 否 | `zh / yue / en / ja / ko`，默认 `zh` |
| `rights_declared` | bool | 是 | 必须为 `true` |
| `rights_note` | string | 否 | 授权备注 |
| `transcribe` | bool | 否 | 是否自动转写 |
| `speaker_count` | int | 否 | 留空自动；1–12 |

上传后会立即启动后台分析。

### 7.2 会话列表/详情

```http
GET /api/media/sessions
GET /api/media/sessions/{session_id}
GET /api/media/sessions/{session_id}/job
```

### 7.3 重新分析

```http
POST /api/media/sessions/{session_id}/analyze
Content-Type: application/json
```

```json
{
  "speaker_count": 2,
  "transcribe": true,
  "model": "large-v3"
}
```

`speaker_count` 可为 `null`，表示自动估计。

### 7.4 选择说话人

```http
POST /api/media/sessions/{session_id}/select-speaker
```

```json
{ "speaker_id": "spk_01" }
```

### 7.5 导出数据集

```http
POST /api/media/sessions/{session_id}/export
```

```json
{
  "speaker_id": "spk_01",
  "include_failed": false,
  "require_text": true
}
```

### 7.6 写入训练工作区

```http
POST /api/media/sessions/{session_id}/activate
```

请求体同上。返回：

```json
{
  "message": "已写入训练工作区：N 句。回到训练页即可做特征提取。",
  "copied": 48,
  "rejected": 4,
  "list": "...\\custom_data\\asr\\sliced_corrected.list",
  "whisper_list": "...\\custom_data\\asr\\sliced_whisper.list",
  "backup": "...\\sliced_corrected.list.bak-...",
  "whisper_backup": "...\\sliced_whisper.list.bak-...",
  "text_ready": true,
  "speaker_id": "spk_01"
}
```

### 7.7 数据集文件下载

```http
GET /api/media/sessions/{session_id}/dataset/{dataset_id}/dataset.list
GET /api/media/sessions/{session_id}/dataset/{dataset_id}/manifest.json
GET /api/media/sessions/{session_id}/dataset/{dataset_id}/checksums.sha256
```

---

## 8. 存储与数据契约

### 8.1 会话目录

```text
<gpt_sovits_root>/custom_data/media/<session_id>/
  session.json
  source/<original>
  analysis/source_16k.wav
  analysis/source_32k.wav
  segments/<run_id>/          # 每次分析一个版本，只增不删
    seg_0001.wav
    segments.json             # 该次分析的冻结片段列表（含转写）
  dataset/<dataset_id>/
    dataset.list
    manifest.json
    checksums.sha256
```

`run_id` 形如 `run_20261017_212650_ab12`。旧会话（切片直接放在 `segments/seg_*.wav`）被视为隐式 `run_legacy`，不迁移、不改写磁盘。

### 8.2 `session.json`

关键字段：

```json
{
  "schema_version": 1,
  "session_id": "media_20261017_153000_ab12",
  "name": "第 3 集",
  "language": "zh",
  "created_at": "...",
  "updated_at": "...",
  "rights": {
    "declared": true,
    "note": "自有素材",
    "declared_at": "..."
  },
  "source": {
    "filename": "ep03.mp4",
    "path": "source/ep03.mp4",
    "analysis_audio": "analysis/source_16k.wav",
    "export_audio": "analysis/source_32k.wav",
    "duration_seconds": 1830.4,
    "codec": "aac",
    "sample_rate": 44100,
    "channels": 2,
    "size_bytes": 0,
    "sha256": "..."
  },
  "job": {
    "status": "idle | running | done | fail",
    "stage": "检测语音",
    "progress": 0.42,
    "message": "...",
    "error": ""
  },
  "speakers": [],
  "segments": [],
  "runs": [
    {
      "run_id": "run_20261017_212650_ab12",
      "created_at": "...",
      "speaker_count": 2,
      "segment_count": 48,
      "duration_seconds": 312.4,
      "path": "segments/run_20261017_212650_ab12"
    }
  ],
  "active_run": "run_20261017_212650_ab12",
  "selected_speaker": "",
  "dataset": null
}
```

`segments` 始终等于 `active_run` 的片段列表（兼容旧接口）；历史 run 用 `segments/<run_id>/segments.json` 读取。

### 8.3 Segment

```json
{
  "segment_id": "seg_0001",
  "speaker_id": "spk_01",
  "start": 12.34,
  "end": 16.78,
  "duration_seconds": 4.44,
  "path": "segments/seg_0001.wav",
  "audio_sha256": "...",
  "text": "今天天气真不错。",
  "qc": {
    "status": "ok | warn | fail",
    "blocking": [],
    "warnings": [],
    "metrics": {}
  }
}
```

### 8.4 Dataset manifest

```json
{
  "schema_version": 1,
  "dataset_id": "ds_...",
  "session_id": "media_...",
  "speaker_id": "spk_01",
  "speaker": {},
  "created_at": "...",
  "language": "zh",
  "source": {},
  "rights": {},
  "stats": {
    "segment_count": 48,
    "duration_seconds": 312.4,
    "train": 40,
    "val": 8,
    "rejected": 4
  },
  "split": {"train": [], "val": []},
  "rejected": [],
  "segments": [],
  "train_ready": true
}
```

---

## 9. 算法细节

### 9.1 VAD

- 25ms 帧 / 10ms hop
- 噪声底：15% 分位
- 峰值：95% 分位
- 阈值：`max(噪声底 + 12dB, 峰值 - 35dB, -55dB)`
- 5 帧中值滤波
- 合并小于 250ms 的间隙
- 丢弃小于 350ms 的语音段

### 9.2 说话人聚类

- 2 秒窗口 / 1 秒 hop
- ERes2NetV2 speaker embedding
- L2 归一化
- `AgglomerativeClustering(metric="cosine", linkage="average")`
- 自动模式距离阈值：0.35（约 0.65 cosine similarity）
- 手动模式直接指定说话人数
- 小于 `max(2, 窗口数/25)` 的簇合并到最近的大簇
- 同说话人相邻 turn 合并；过短 turn（<2s）在可能时并入相邻同说话人 turn

### 9.3 切片

- 2–10 秒
- 长 turn 在目标切点 ±700ms 内找最低能量帧
- 前后保留 80ms
- 32kHz / PCM16 单声道

### 9.4 QC

- 直接调用 `reference_audio.analyze(path, purpose="synthesis")`
- QC 失败默认不进入数据集
- QC 只做确定性 DSP，不检测 BGM 或多说话人重叠

### 9.5 ASR

- faster-whisper
- `beam_size=1`
- `vad_filter=False`（已经切好）
- 失败时写 `text_error`，不中断分析

---

## 10. 训练接入

### 10.1 写入的文件

```text
<gpt_sovits_root>/custom_data/sliced/media_<session_id>_<segment_id>.wav
<gpt_sovits_root>/custom_data/asr/sliced_corrected.list
<gpt_sovits_root>/custom_data/asr/sliced_whisper.list
```

旧清单会备份为：

```text
sliced_corrected.list.bak-<timestamp>-<uuid>
sliced_whisper.list.bak-<timestamp>-<uuid>
```

### 10.2 训练状态

`/activate` 会调用 `mark_step`：

- `slice` → done
- `asr` → done（仅当所有片段都有文本）
- `correct` → done

因此训练页可以直接进入“特征提取”。

### 10.3 注意

- 旧切片文件不会被删除，只切换标注清单
- `training_stats` 的 `sliced` 数量会统计 `custom_data/sliced/` 下所有 WAV，可能包含旧切片
- 导出的 `dataset.list` 使用会话目录里的绝对路径；直接拿它训练时，prepare 脚本会按 basename 到 `inp_wav_dir` 查找，所以正式训练请走 `/activate`，不要只下载 `dataset.list`

---

## 11. 测试与验证

### 11.1 媒体后端测试

```powershell
& .\.venv\Scripts\python.exe -m unittest backend.tests.test_media_ingest -v
```

结果：**15/15 通过**

覆盖：

- 会话创建/列表
- 权利声明门禁
- session_id 路径穿越
- 上传 + 16k/32k 音轨
- 单会话只接收一个媒体
- VAD 找到两段
- 两说话人聚类
- 长 turn 切片边界
- QC 字段
- 导出 list/manifest/checksums
- 未转写时拒绝导出
- 选择不存在说话人
- API 上传/分析/选择/导出/activate

### 11.2 真实双说话人冒烟

使用 Windows SAPI 生成男声/女声两段中文，拼接后跑真实 ERes2NetV2：

- 识别到 2 个说话人
- 生成 2 个切片
- QC 均为 `ok`
- faster-whisper 转写成功

### 11.3 前端

```powershell
cd G:\Clone-youself\frontend
npx vitest run src/components/MediaIngestPanel.test.ts src/components/IconRail.test.ts
npm run build
```

结果：

- 采集页 + 导航测试：**14/14 通过**
- `npm run build`：**通过**

### 11.4 Playwright 端到端

已实际走通：

```text
上传 combined.wav
→ 自动分析
→ 显示 2 个说话人
→ 选择说话人
→ 导出训练集
→ 写入训练工作区
```

截图：

```text
G:\Clone-youself\output\playwright\media-ingest.png
```

### 11.5 当前全量测试状态

交接时记录的“与本功能无关的失败”已被后续改动修掉，全量测试现为全绿。

**后端全量**：

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s backend\tests -p "test_*.py" -v
```

结果：**303 项全过**（含并发媒体采集修复、异步建档、发音清单、GPU 租约、数据集版本化与任务注册表的测试）。

**前端全量**：

```powershell
cd frontend
npm test -- --run
npm run build
```

结果：**125 项全过**，`npm run build` 通过。

---

## 12. 已知问题 / 技术债

1. **一个会话一个媒体文件**
   批量上传应拆成多个会话，或后续加 batch API。

2. **不做音源分离**
   默认从原音轨切片。BGM 重时，QC 可能通过但音色仍被音乐污染；后续可加可选 Demucs/UVR 阶段，但要保留原始片段并标记 `processed`。

3. **重叠说话不保证正确归属**
   当前是 diarization + 时间投票，不是真正的多说话人分离。

4. **角色变声会降低聚类准确率**
   动漫角色变声、音效、回声都可能被当成额外说话人。

5. **说话人 ID 非持久**
   `spk_01`、`spk_02` 是每次分析按总时长排序生成的；重新分析后可能交换。`speaker_id` 只在一个会话内有效。

6. **没有手动合并/拆分片段 UI**
   目前只能整体重新分析，或修改 `session.json`（不建议手改）。

7. **没有人工校对转写 UI**
   转写结果可以在训练页旧的校对流程里看，但采集页本身不提供逐句改字。

8. **GPU/VRAM 竞争**
   ERes2NetV2 默认优先 CUDA；与合成/训练同时跑可能 OOM。当前没有显存排队/自动降级。

9. **dataset.list 不是直接训练入口**
   正式训练请用 `/activate`，它会复制切片并写标准清单。

10. **旧切片不会自动清理**
    `/activate` 只切换清单，不删除 `custom_data/sliced/` 里的旧文件。

11. **长视频性能**
    30 分钟以上视频的 embedding 窗口数和 QC 片段数会明显增加；进度已做 2% 节流，但整体仍是单进程后台线程。

12. **没有任务取消**
    媒体分析任务目前不能中途停止；训练页的 stop 不适用于媒体分析。

---

## 13. 下一步建议

按优先级：

1. **片段人工复核**
   增加“排除/恢复片段”“合并相邻片段”“手动改说话人”操作。

2. **转写校对**
   采集页直接显示文本输入框，保存后更新 `session.json` 和导出 manifest。

3. **批量上传**
   一次选择多个文件，每个文件一个 session，队列串行分析。

4. **可选人声分离**
   只对 BGM 重的素材启用；输出 `processed` 版本并保留原始片段，避免污染音色。

5. **GPU/CPU 设备选择**
   在分析接口增加 `device` 参数，默认自动；显存不足时提示切换 CPU。

6. **任务取消**
   为媒体分析增加 stop 接口和线程取消标记。

7. **DatasetVersion 与 License Ledger 打通**
   把 `manifest.json` 的 `rights` 升级为正式 `ConsentRecord / License` 快照，并接入授权账本。

8. **远程演员采集**
   邀请链接、手机端录音、断点续传、运营审核台。

---

## 14. 待提交文件与建议 commit

### 14.1 本次媒体采集相关文件

新增：

```text
backend/services/media_ingest.py
backend/services/media_diarization.py
backend/tests/test_media_ingest.py
frontend/src/components/MediaIngestPanel.vue
frontend/src/components/MediaIngestPanel.test.ts
```

修改：

```text
backend/app.py
backend/schemas.py
frontend/src/App.vue
frontend/src/components/IconRail.vue
frontend/src/components/IconRail.test.ts
frontend/src/layout.test.ts
frontend/src/components/TimbrePanel.test.ts
docs/ARCHITECTURE.md
docs/PROJECT_MEMORY.md
```

注意：`backend/app.py`、`frontend/src/App.vue` 和两个 docs 文件里可能混有其他未完成改动，提交前请按 hunk 审查。

### 14.2 建议 commit 摘要

```text
新增媒体采集页：上传媒体、说话人分段、句级切片与训练集导出
```

### 14.3 建议描述

```markdown
## 使用体验

- **上传即可分析**：上传本地视频或音频后，自动提取音轨、检测语音并聚类说话人。
- **按角色取素材**：试听说话人样例，选择目标说话人，只导出该角色的片段。
- **切片可质检**：每段保留时间戳、转写和 QC 结果，质检失败片段默认排除。
- **直接接入训练**：一键写入切片目录和标注清单，备份旧清单后即可做特征提取。

## 技术说明

- 新增 MediaIngestService 与 diarization 模块：ffmpeg 双采样率音轨、VAD、ERes2NetV2 embedding、层次聚类、2–10 秒切片。
- 新增 /api/media/sessions 系列接口和“采集”前端页。
- 可选 faster-whisper 转写；训练步骤 slice/asr/correct 自动标记完成。
- 上传必须声明素材用于训练的授权；默认不做音源分离。
```

---

## 15. 故障排查

### 15.1 `找不到 ffmpeg/ffprobe`

确认：

```powershell
where.exe ffmpeg
where.exe ffprobe
```

### 15.2 `说话人验证模型不存在`

确认 checkpoint：

```text
G:\Clone-youself\GPT-SoVITS\GPT_SoVITS\pretrained_models\sv\pretrained_eres2netv2w24s4ep4.ckpt
```

### 15.3 转写不可用

检查本地 faster-whisper 模型是否完整：

```powershell
& .\.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'scripts'); from run_whisper import resolve_local_model; print(resolve_local_model('large-v3'))"
```

### 15.4 `没有检测到可用语音`

- 文件是否只有音乐
- 音量是否过低
- 是否被 VAD 阈值全部过滤
- 可尝试先做音量归一化或换更干净的素材

### 15.5 所有片段 QC `fail`

常见原因：

- 窄带来源（电话音质、8k 升采样）
- 片段太短（<1.5s）
- 削波
- 静音过多
- 电平过低

UI 可勾选“包含质检失败片段”先导出检查，但不建议直接用于训练。

### 15.6 CUDA OOM

- 先停止合成/训练任务
- 或把 `SpeakerEmbedder` 切到 CPU（当前代码默认优先 CUDA，需要后续加 `device` 参数）
- 长视频建议分段上传

---

## 16. 合规边界

只接受：

- 自己的声音/素材
- 已签约配音演员的授权素材
- 版权方明确许可的素材
- 明确允许训练使用的公开数据集/CC 授权素材

不接受：

- 无授权爬取某个动漫角色/配音演员声线
- 绕过平台下载限制
- 把“公开可看”当成“可训练/可商用”

---

## 17. 交接检查清单

- [x] 采集页可从主导航进入
- [x] 上传必须勾选权利声明
- [x] 上传后自动分析
- [x] 说话人卡片可试听/选择
- [x] 片段有 QC 和转写
- [x] 可导出 manifest/list/checksums
- [x] 可写入训练工作区并备份旧清单
- [x] 媒体相关后端测试通过
- [x] 采集页前端测试通过
- [x] 前端 build 通过
- [x] 真实双说话人冒烟通过
- [x] 全量测试无失败（后端 303 项、前端 125 项全过）
- [ ] 远程演员采集（未做）
- [ ] 音源分离（未做）
- [ ] 多租户/License Ledger（未做）

---

## 18. 测试残留清理

验证过程中写入的测试产物已在 2026-10-17 后续接手时移入备份目录（没有硬删除）：

```text
G:\Clone-youself\TEMP\handover_cleanup\20261017_media_smoke\sliced\media_media_20261017_203059_ab974c_seg_0002.wav
G:\Clone-youself\TEMP\handover_cleanup\20261017_media_smoke\media\media_20261017_203059_ab974c\
G:\Clone-youself\TEMP\media_smoke\
G:\Clone-youself\output\playwright\media-ingest.png
```

其中：

- `TEMP/`、`output/` 已在 `.gitignore` 中，不会进入提交
- 移动前已确认这两项不被 `sliced_corrected.list`、`sliced_whisper.list`、`sliced.list` 中任何一行引用
- `custom_data/sliced/` 已无 `media_*` 残留，`custom_data/media/` 已无残留会话，`training_stats.sliced` 不再虚高
- 原来的 `custom_data/asr/sliced_corrected.list` 保持 **64 行 / 8613 字节**，hash 与备份一致
- `custom_data/asr/sliced_corrected.list.bak-20261017_203135` 保留为回滚点，不要删
- 如需还原，把 `TEMP\handover_cleanup\20261017_media_smoke` 下两项移回原路径即可

---

## 19. 最后提醒

- 不要替用户执行 `git commit` / `git push`
- 不要删除 `custom_data/raw/`、`sliced/`、`asr/` 和最终权重
- 不要把媒体采集改动和当前工作区其他未完成改动混成一个 commit，除非逐 hunk 确认

---

## 20. 接手后修复（2026-10-17 晚）

交接时记录的三个缺陷与技术债已修，全部有测试覆盖：

| 问题 | 修法 | 测试 |
|---|---|---|
| 分析互斥失效：`_media_service()` 每请求新建实例，`self._lock` 不跨请求，两个并发 `/analyze` 可能都启动分析线程 | 模块级 `_ACTIVE_ANALYSES` + `_ANALYSIS_LOCK`，在锁内完成“检查 + 写 job=running + 登记”，线程 `finally` 按 token 清理；线程启动失败也回滚登记 | `test_running_analysis_rejects_second_call_and_cleans_registry`、`test_concurrent_callers_have_exactly_one_winner` |
| 上传失败留下空壳会话：先建目录再校验后缀，失败后 `list_sessions` 多一条空记录 | `ingest_file` 包一层，异常时重新 load 确认 `source` 为空才删除会话目录；已有 source 的会话绝不删；原异常原样抛出 | `test_rejected_upload_removes_the_new_empty_session`、`test_io_failure_also_removes_the_new_empty_session`、`test_failed_second_upload_never_deletes_a_session_with_source` |
| 媒体转写不做繁→简，与主线 `voice_ingest` 不一致 | `_transcribe` 复用 `voice_ingest.normalize_transcript(text, language=session_language)` | `test_traditional_whisper_text_is_written_as_simplified` |
| 偶发 `session.json.tmp -> session.json` `WinError 5`：分析线程写进度时，另一个线程正好打开 `session.json` 读取，Windows 拒绝 `os.replace` | `_atomic_json` 改为唯一临时名 + `_replace_with_retry`（退避重试，最多 12 次） | `test_save_retries_while_a_reader_holds_the_file` |
| `tools/rules.py` 文件末尾缺换行 | 补 LF | `git diff --check` |

行为细节：

- 繁转简按**会话语言**判断，`yue` 不做转换、保留繁体——粤语的「係」不能变成「系」，这是 2026-10-17 确认的产品决定，与主线 `transcribe_reference` 一致；有测试锁死（`test_cantonese_keeps_traditional_characters`）。
- 并发分析只在**同一进程内**互斥。进程崩溃后 `session.json` 里的 `job.status` 可能残留 `running`，会阻止再次分析；这是原有行为，见第 12 节技术债。
- 测试残留已按第 18 节移入 `TEMP/handover_cleanup/`，`custom_data/sliced/` 与 `custom_data/media/` 已无冒烟产物。
