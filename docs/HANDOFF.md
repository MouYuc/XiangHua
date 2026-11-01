# 项目交接文档 · 2026-10-17（**已过期，仅作历史记录**）

> [!WARNING]
> **这份文档描述的是 2026-10-17 的状态，已经与代码不符，不要按它接手。**
>
> 仍然正确但已过时的内容举例：文中写的分支 `New-Start` 已不存在（当前分支 `Referctor`）、
> 提交号 `0942eea` 早已不是 HEAD、WebUI 的六个页面已被「工作台 / 声音库 / 记录」三目的地取代、
> `backend/app.py` 的 76 条路由已拆进 `backend/api/routers/`、`core.py` 已拆成
> `domain / engine / pipeline / services` 四层。
>
> **当前事实来源（按优先级）：**
> 1. [`docs/REFACTOR_2026-11.md`](REFACTOR_2026-11.md) —— 架构分层、缺陷清单、验证方式
> 2. [`docs/PROJECT_MEMORY.md`](PROJECT_MEMORY.md) —— 项目级持久记忆，每次会话开始先读
> 3. `README.md` —— 安装、运行、测试与目录结构
>
> 下方原文保留，只用于回溯历史决策。

---

## 历史原文（2026-10-17）

> 项目：像话 XiangHua · 角色声线平台  
> 交接对象：下一位接手开发/验证的工程师  
> 当前分支：`New-Start`  
> 最新提交：`0942eea 清场并重建新项目起点：四分离架构、B2B 策略与清晰优先基线`  
> 远程关系：领先 `origin/main` 9 个提交；本次交接内容**尚未提交、未 push**  
> 工作区状态：有大量未提交改动；其中一部分是并发的媒体采集功能，必须分开审查

---

## 1. 一句话现状

项目已经从“广普 TTS 工具”转向：

> **面向独立游戏与短剧团队的“角色声线 API + 训练能力 + 授权账本”。**

当前本地 MVP 已经能完成：

- 上传一段自然说话音频；
- 自动切片、选参考片段、跑质量门禁、自动 ASR；
- 用清晰优先底模合成；
- 用 `off / light / medium / heavy` 四档控制口音；
- 用 20 句固定回归和同文本四档交换测试做客观验收。

但**还没有**进入 B2B API 平台化，也没有完成真正的音色 adapter 训练。

---

## 2. 最重要的架构修正：不要再要求用户“补录”

### 2.1 正确原则

用户只提供**一段正常的自然说话音频**。

可以是：

- 访谈、播客、会议、语音备忘；
- 已授权的配音素材；
- 手机自然说的一段话；
- 客户已有的角色录音。

不需要：

- 照读台词本；
- 为“清晰普通话 / 轻 / 中 / 浓口音”分别补录；
- 手工逐字填写参考文本；
- 标注发音、韵律或口音；
- 为了训练重新配音。

### 2.2 四层分离仍然是硬约束

| 层 | 只负责 | 数据来源 |
|---|---|---|
| 音色层 | 说话人身份/音色 | 自然音频，仅用于 speaker encoder/adapter |
| 发音层 | 字→音素、多音字、口音读音 | 字典 + G2P |
| 韵律层 | 停顿、重音、语调、情绪 | 多说话人标准语料/风格标签 |
| 口音层 | 音素替换、语调节奏 | 独立 accent pack |

目标音频**不能**用于学习口音、韵律和内容。  
之前“SECS 很高但听不清”的根因，就是把这几层纠缠进了 GPT-SoVITS 全量微调。

### 2.3 正确输入流水线

```text
一段自然说话音频
  → 自动 VAD / 切片
  → 质量门禁
  → 自动 ASR
  → 自动选 2–10 秒参考片段
  → VoiceAsset（原始音频 + 切片 + 转写 + 质量报告 + 授权记录）
  → 合成参考 / 后续音色 adapter
```

用户只在 ASR 有明显错字时做**可选修正**，不把人工转写作为默认前置步骤。

---

## 3. 本次交接窗口完成的内容

### 3.1 音色入口：一段自然音频自动建档

新增 `backend/services/voice_ingest.py`：

- 读取自然录音；
- VAD 检测语音区域；
- 合并短停顿，优先选择 4–8 秒的完整片段；
- 跑 `reference_audio` 质量门禁；
- 用本地 faster-whisper 自动转写；
- 繁体中文自动转简体；
- 返回 `ref_audio + ref_text + quality + selected`。

`POST /api/reference-audio` 默认 `auto_prepare=true`：

- 原始上传保留；
- 另存自动选出的参考片段；
- WebUI 自动填入音频路径和识别文本；
- 用户不需要手工填写参考原文。

异步接口（新增，长录音不再阻塞 HTTP）：

- `POST /api/reference-audio/prepare` 秒回 `{job_id, status}`，服务端在后台线程做切片/门禁/ASR；
- `GET /api/reference-audio/prepare/{job_id}` 返回快照（`status / phase / progress / stage / result / error`）；
- 前端 700ms 轮询，按阶段显示“语音转写 85%”，完成才写配置；组件卸载或重复上传会停掉旧轮询；
- 同步 `POST /api/reference-audio` 保留，供音色池/歌声等只做质检的用途。

真实冒烟：

- 3.9 秒自然片段：选中 3.9 秒，ASR 成功，质量门禁 `ok`；
- 5 分 44 秒原始录音：选中 5.24 秒窗口，ASR 成功，质量门禁 `ok`；
- 同步路由真实返回 `200`、`auto_prepared=true`；
- 异步路由真实返回 `200`，7.0 秒完成，选中 5.24 秒窗口、质量门禁 `ok`、原文自动填入。

### 3.2 发音层档位化

新增 `backend/services/pronunciation.py`：

- `off / light / medium / heavy`；
- 长词优先；
- `目的 / 的确 / 于是 / 可是 / 但是 / 有的 / 是的 / 要是 / 总是 / 只是 / 还是` 在渲染时保护；
- `light/medium` 不做全局“的/是”替换；
- `heavy` 才做全局替换；
- 旧 `tools/rules.py` 的 `to_lightmid()` 保留为 heavy 兼容入口；
- 旧 `AUTO_LIGHTMID=1` 映射为 `medium`，不再等价于全局替换。

发音计划持久化（新增）：

- `core.render_with_plan()` 是唯一构建入口，`render_synthesis_text()` 只取 `rendered_text`；
- 每句结果 `output["pronunciation"]` 带完整 `PronunciationPlan.to_dict()`；
- 输出目录写 `pronunciation.json`（schema_version / accent_level / lexicon_id / accent_pack_id / accent_intensity / protected_terms / lines）；
- manifest 写失败只记 `pronunciation_manifest_error`，不会让合成失败。

### 3.3 参考音/文本配对

新增 `backend/services/reference_prompt.py`：

- 读取 `sliced_corrected.list`、`sliced_whisper.list`、`sliced.list`；
- 校正表优先；
- 支持路径规范化、跨盘符文件名兜底；
- WebUI 选择已知切片时自动带出原文；
- 明显不匹配会在合成前拒绝；
- 自定义上传音频现在由 `voice_ingest` 自动转写。

### 3.4 回归与交换测试

新增：

- `scripts/tts_regression.py`：20 句固定回归；
- `backend/tests/fixtures/tts_regression_20.json`：5 组 × 4 句；
- `scripts/accent_swap_verify.py`：同文本、只改口音档位的交换测试。

### 3.5 文档与测试依赖

- 新增 `docs/MVP_VALIDATION.md`；
- 更新 `README.md`、`docs/ARCHITECTURE.md`、`docs/TRAINING_GUIDE.md`、`docs/USAGE_GUIDE.md`、`docs/WEBUI_GUIDE.md`、`docs/B2B_VOICE_API_STRATEGY.md`；
- 新增 `requirements-dev.txt`，测试环境已补装 `pytest`。

### 3.6 核心架构收敛（2026-10-17 晚）

详细记录见 `docs/CORE_ARCHITECTURE_AUDIT.md`。要点：

- **GPU 租约**：`backend/services/gpu_arbiter.py`，合成/模型加载/媒体分析/单音频建档/音色转换/训练共用一把进程级租约（FIFO，默认 60 秒超时，训练等长任务不超时）。`/api/overview` 暴露 `gpu: {busy, holder, queue, vram}`。
- **数据集版本化**：媒体分析改为 `segments/<run_id>/`，只增不删；`export_dataset(run_id=)` 可导出历史 run；`activate_dataset(dataset_id=)` 可原样重放任一历史数据集。
- **任务注册表**：`backend/services/job_registry.py` 聚合五套状态，`GET /api/jobs`、`GET /api/jobs/{id}`、`POST /api/jobs/{id}/cancel`（目前仅训练可取消）。
- **自检 8 问**：6 项变绿，环境切换（需重启）与任务持久化（有意保留）仍是已知缺口。
- **API 版本化**：`/api` 与 `/v1` 双挂载同一套 handler；未知 API 路径返回 JSON 404；错误响应统一带 `code`（详见 `docs/API_V1.md`）。
- **许可证**：`LICENSE` 为 AGPL-3.0。

---

## 4. 当前代码地图

| 文件 | 作用 | 交接状态 |
|---|---|---|
| `backend/services/voice_ingest.py` | 单音频自动切片/ASR/选参考片段 | 新增，已验证；带进度回调 |
| `backend/services/voice_prepare_jobs.py` | 单音频自动建档的异步 job | 新增，已验证 |
| `backend/services/pronunciation.py` | `PronunciationPlan` 与四档口音规则 | 新增，已验证 |
| `backend/services/reference_prompt.py` | 参考原文查询/配对校验 | 新增，已验证 |
| `backend/services/reference_audio.py` | DSP 质量门禁 | 既有，继续使用 |
| `backend/services/core.py` | 合成核心、配置、任务 | 已接入档位化发音层 + `pronunciation.json` |
| `backend/app.py` | FastAPI 路由 | 新增自动建档（同步 + 异步）、配对与媒体接口 |
| `frontend/src/App.vue` | WebUI | 上传后异步建档、阶段轮询、自动填入原文 |
| `scripts/tts_regression.py` | 20 句固定回归 | 新增，已验证 |
| `scripts/accent_swap_verify.py` | 同文本四档交换 | 新增，已验证 |
| `scripts/five_language_verify.py` | 五语种验收 | 新增 `use_itn` 控制 |
| `backend/services/media_ingest.py` | 媒体采集/说话人聚类/数据集导出 | 并发加入；已修并发互斥/空会话/繁简/原子写；分析改为 append-only run 版本 |
| `backend/services/media_diarization.py` | VAD/说话人聚类/切片 | 并发加入，单独审查 |
| `frontend/src/components/MediaIngestPanel.vue` | 媒体采集页 | 并发加入，单独审查 |
| `backend/services/gpu_arbiter.py` | 进程级 GPU 租约（合成/训练/分析/音色） | 新增，已测；见 `docs/CORE_ARCHITECTURE_AUDIT.md` |
| `backend/services/job_registry.py` | 五套任务状态的只读聚合门面 | 新增，已测；`GET /api/jobs` |

知识图谱 `.code-review-graph/graph.db` 当前不存在；本次按源码、测试和真实音频核验。若后续重建图谱，先用图谱定位，再用源码复核。

---

## 5. 当前配置与运行环境

### 5.1 清晰优先基线

```text
GPT 模型:   GPT_SoVITS/pretrained_models/s1v3.ckpt
SoVITS:     GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth
参考音:     GPT-SoVITS/custom_data/sliced/训练.wav_0000018880_0000143680.wav
参考文本:   今天天气真不错，我们出去走走吧。
口音档位:   off
旧开关:     auto_lightmid=false
```

### 5.2 环境

- Python 3.10.20；
- PyTorch 2.6.0+cu126，CUDA 可用；
- `funasr`、`librosa`、`soundfile`、`faster-whisper`、`opencc` 可用；
- 本机为 Windows，命令请优先使用 PowerShell。

### 5.3 启动

```powershell
.\setup.ps1
& .venv\Scripts\python.exe tools\webui.py
```

默认本地地址：`http://127.0.0.1:7860`。

---

## 6. 验证快照

### 6.1 自动测试

| 项目 | 结果 |
|---|---|
| 后端 unittest | **293 tests，0 failures，0 errors** |
| 前端 Vitest | **125 tests，全过** |
| `npm run build` | 通过 |
| `compileall` | 通过 |
| `git diff --check` | 通过 |

### 6.2 20 句真实回归

报告：`output/tts_regression/report.json`

| 组 | CER 均值 | SECS 均值 |
|---|---:|---:|
| clear | 3.12% | 0.7601 |
| technical | 0.00% | 0.7680 |
| light | 16.09% | 0.7783 |
| medium | 22.46% | 0.7974 |
| heavy | 31.18% | 0.8091 |

全量：20/20 成功，0 削波，0 可疑短音频。

### 6.3 同文本四档交换

报告：`output/accent_swap/report.json`

| 档位 | CER | SECS |
|---|---:|---:|
| off | 3.33% | 0.8050 |
| light | 3.45% | 0.8012 |
| medium | 13.79% | 0.7791 |
| heavy | 20.69% | 0.7948 |

结论：口音变重时 CER 明显上升，SECS 基本稳定。**SECS 单独不能验收。**

### 6.4 测量修正

已修掉两个会误判的评测问题：

1. SenseVoice 的 `<|zh|><|NEUTRAL|><|Speech|>` 控制 token 不再计入 CER；
2. CER 评测关闭 ASR ITN，避免“三点”被转成“3点”造成假错误。

---

## 7. 当前已知问题与风险

1. `heavy` 档仍会全局替换“的/是”，只适合压力测试，不应作为默认生产路径。
2. G2P 补丁仍在 `GPT_SoVITS/text/chinese2.py` 和训练特征里，尚未彻底外置到版本化字典。
3. 旧训练集 64 条、281.7 秒，其中 52 条/226.8 秒是浓广普；模型会把浓口音学进音色。
4. 自动 ASR 仍可能出错，所以参考文本保留“可修正”，但不要求用户逐字输入。
5. 单音频自动建档已改异步 job：`POST /api/reference-audio/prepare` 秒回 job_id，切片/门禁/ASR 在后台线程跑，前端按阶段轮询。job 只存进程内存，重启或多 worker 会丢，需重新上传。
   - 这是**有意保留的架构缺口**：等要跨重启/多 worker 时，只替换 `VoicePrepareJobManager` 的存储层（`start/snapshot/update` 三个方法背后的表）即可，路由契约和前端轮询不用动，模块 docstring 已写明边界。
6. 单音频入口默认按单说话人处理；多人素材走 `media_ingest` 的说话人聚类。
7. `seed` 已写入回归配置，但核心尚未真正读取；复现性仍依赖核心的确定性种子。
8. 回归只覆盖一套音色；换音色、换信道等交换测试还没完成。
9. B2B API 仍缺：API Key、租户、配额、持久化队列、项目/角色、voice version、license/consent、Webhook、水印、SDK。
10. `.worktrees/` 里还有两个旧 worktree 有未提交改动，未经用户确认不要删除。

---

## 8. 工作区与提交注意事项

### 8.1 当前状态

- 分支：`New-Start`
- 最新提交：`0942eea`
- 本次交接内容：**全部未提交**
- 未执行 `git commit`、`git push`
- `output/` 和 `.guangpu_webui/` 下是运行产物，已在 `.gitignore` 中

### 8.2 必须分开审查的并发改动

工作区里同时存在并发的媒体采集功能：

- `backend/services/media_ingest.py`
- `backend/services/media_diarization.py`
- 单独的媒体采集交接：`docs/MEDIA_INGEST_HANDOVER.md`
- `backend/tests/test_media_ingest.py`
- `frontend/src/components/MediaIngestPanel.vue`
- `frontend/src/components/MediaIngestPanel.test.ts`
- `IconRail` 的 `media` 导航改动

这些不是本次“单音频自动建档”的核心改动。提交前请分开 review，不要直接 `git add -A` 一把梭。

### 8.3 本次交接相关文件

新增：

```text
backend/services/voice_ingest.py
backend/services/pronunciation.py
backend/services/reference_prompt.py
backend/tests/test_voice_ingest.py
backend/tests/test_pronunciation.py
backend/tests/test_pronunciation_integration.py
backend/tests/test_reference_prompt.py
backend/tests/test_tts_regression.py
backend/tests/test_accent_swap_verify.py
backend/tests/fixtures/tts_regression_20.json
scripts/tts_regression.py
scripts/accent_swap_verify.py
docs/MVP_VALIDATION.md
requirements-dev.txt
```

修改（与并发改动可能同文件）：

```text
README.md
backend/app.py
backend/schemas.py
backend/services/core.py
backend/tests/test_synthesis_reverse.py
frontend/src/App.vue
frontend/src/api.ts
frontend/src/layout.test.ts
frontend/src/styles.css
tools/local_tts.py
tools/rules.py
scripts/five_language_verify.py
docs/ARCHITECTURE.md
docs/B2B_VOICE_API_STRATEGY.md
docs/PROJECT_MEMORY.md
docs/TRAINING_GUIDE.md
docs/USAGE_GUIDE.md
docs/WEBUI_GUIDE.md
assets/广普训练台词本.txt（仅加“可选示例”声明）
```

---

## 9. 建议的提交拆分

建议至少拆成三个逻辑提交：

1. **发音层与回归验收**
   - `pronunciation.py`、`tools/rules.py`、`core.py`、`schemas.py`、回归脚本和测试、MVP 文档。
2. **单音频自动建档**
   - `voice_ingest.py`、参考音配对、`/api/reference-audio`、WebUI 自动填原文、相关测试。
3. **并发媒体采集**
   - `media_ingest.py`、`media_diarization.py`、`MediaIngestPanel`、`IconRail` 相关改动。

建议第一条提交摘要：

```text
新增 TTS 清晰优先 MVP 验证：档位化发音层、单音频自动建档与 20 句回归

## 使用体验

- **只上传一段自然说话音频**：系统自动切片、识别原文并选参考片段，不需要照读台词本。
- **口音可控**：off/light/medium/heavy 四档，固定搭配不会被全局替换破坏。
- **验收有据**：20 句回归同时看 CER、SECS、削波和异常静音。

## 技术说明

- 新增 PronunciationPlan、voice_ingest、参考音配对和回归台架。
- CER 评测去除 SenseVoice 控制 token，并关闭 ASR ITN。
- 口音加重时 CER 上升、SECS 基本稳定，SECS 不能单独验收。
```

---

## 10. 下一步优先级

### P0：接手后先做

1. 读 `AGENTS.md`、`docs/PROJECT_MEMORY.md`、本文档。
2. 用一段真实自然音频在 WebUI 走一遍“上传 → 自动识别 → 合成”。
3. 确认 `output/tts_regression/report.json` 与 `output/accent_swap/report.json` 仍在。
4. 把并发的媒体采集改动和这次音频建档改动分开 review。

### P1：产品主线

1. ~~把 `auto_prepare` 改成异步 job，避免长音频阻塞 HTTP。~~ **已完成**：新增 `voice_prepare_jobs.py` 与 `/api/reference-audio/prepare`，前端阶段轮询；同步接口保留兼容。
2. 训练只做音色 adapter/轻量适配；口音、韵律、发音继续独立。
3. ~~把 `PronunciationPlan` 持久化到输出 manifest。~~ **已完成**：每句 output 带 `pronunciation`，输出目录写 `pronunciation.json`。
4. 实现 `VoiceCard / AccentPack / StylePack` 数据契约。
5. 重跑 20 句回归，目标 clear/technical CER 稳定在 5% 以下。

### P2：平台化

1. Managed Training pilot；
2. B2B API 骨架；
3. license ledger、consent、manifest、Webhook；
4. On-prem 打包。

**TTS 可懂度没有稳定过线前，不要做 P2。**

---

## 11. 接手检查清单

- [ ] 已读 `AGENTS.md` 和 `docs/PROJECT_MEMORY.md`
- [ ] 确认当前分支是 `New-Start`，没有误操作提交/推送
- [ ] 确认 GPT-SoVITS 模型路径和参考音存在
- [ ] 跑通后端 215 条测试
- [ ] 跑通前端 125 条测试和 `npm run build`
- [ ] 跑一遍 `scripts/tts_regression.py --limit 1`
- [ ] 用真实自然音频走一遍 WebUI 自动建档
- [ ] 检查 `docs/HANDOFF.md`、`docs/ARCHITECTURE.md`、`docs/MVP_VALIDATION.md`
- [ ] 分开 review 并发媒体采集改动
- [ ] 不删除 `custom_data/raw/`、`sliced/`、`asr/` 和最终权重
- [ ] 确认素材授权与 `license_id` 方案

---

## 12. 常用命令

```powershell
# 后端测试
.\.venv\Scripts\python.exe -m unittest discover -s backend/tests -p "test_*.py"

# 前端测试与构建
cd frontend
npm test -- --run
npm run build
cd ..

# 20 句回归
.\.venv\Scripts\python.exe scripts\tts_regression.py

# 复用已有音频，只重算指标
.\.venv\Scripts\python.exe scripts\tts_regression.py --skip-synthesis

# 同文本四档口音交换
.\.venv\Scripts\python.exe scripts\accent_swap_verify.py --skip-synthesis

# 单句冒烟
.\.venv\Scripts\python.exe scripts\tts_regression.py --limit 1 --group clear
```

---

## 13. 关键文档

- [项目记忆](PROJECT_MEMORY.md)
- [核心架构自检](CORE_ARCHITECTURE_AUDIT.md)
- [/v1 API 契约](API_V1.md)
- [四分离架构](ARCHITECTURE.md)
- [MVP 验证记录](MVP_VALIDATION.md)
- [B2B API 与授权策略](B2B_VOICE_API_STRATEGY.md)
- [训练指南](TRAINING_GUIDE.md)
- [使用手册](USAGE_GUIDE.md)
- [WebUI 指南](WEBUI_GUIDE.md)
- [媒体采集能力交接](MEDIA_INGEST_HANDOVER.md)

---

## 14. 交接底线

- 用户的声音资产只能来自本人、签约配音演员、版权方或明确许可的素材；
- 不做无授权声线爬取；
- 不要求用户照读台词本或补录特定口音；
- 不把目标音频用于发音、韵律、口音训练；
- 不用 SECS 单独判断 TTS 是否成功；
- 不在可懂度未过线前做 B2B 平台化；
- 不替用户执行 `git commit` / `git push`，除非用户明确委托。
