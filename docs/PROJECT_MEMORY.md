# 项目持久记忆

更新时间：2026-11-21
状态：架构分层与工程化基建完成（见 0j 与 `docs/REFACTOR_2026-11.md`）；清晰优先基线已用本人真实声音验证；2026-11-13 完成磁盘瘦身与结构收敛（0k）及大文件拆分与分层归位（0l）；2026-11-16 前端第三次推倒重建为「设计稿 D · 单屏极简 · 浅色纸面」（0m）；2026-11-17~21 环境抢救 + kNN-VC 恢复 + 工作坊训练链路修复并首次由用户在 UI 全程完成训练（0n）

这份文档是后续会话的项目级记忆入口。开始任务前先读 `AGENTS.md` 和本文件；如果知识图谱已被重建，再检查 `.code-review-graph/graph.db`。当前图谱已在清场时删除，后续按需重建。完整交接见 `docs/HANDOFF.md`。

---

## 0a. 2026-10-25 全新起点（用户指令）

用户确认音色已验收（"音色像了"），指示不再纠结音色相似度，按文档路线往下推。同日按用户指令清除旧训练资产（**备份非硬删**，位于 `TEMP/handover_cleanup/20261024_fresh_start/`，内有 README 与还原方法）：

- 旧 `custom_data/raw|sliced|asr`（64 条切片，52 条浓广普）与 6 个已训练权重（myvoice/guangpu07/guangpu_g2p）已移入备份；原目录重建为空；
- `.guangpu_webui/config.json` 的 `ref_audio/ref_text` 当时清空，随后由新录音自动建档重新填入；
- 底模、`.guangpu_webui/models/`、`reference_audio/`、`timbre/`、`output/`（回归报告）保留未动。

---

## 0b. 2026-10-25 本人声音基线（新参考音）

输入：用户一段 10.4 秒自然说话 wav（44.1k 立体声，手机级电平）。

- `POST /api/reference-audio/prepare` 4.5 秒完成：自动选 6.47 秒窗口、ASR 成功；质量门禁 **fail（电平过低，峰值 -22.2 dBFS）——按设计未拦截**，照样填入配置并可合成；
- 单句试合成 CER 2.94%；
- 20 句回归（`output/tts_regression_myvoice/report.json`，旧基线 `output/tts_regression/report.json` 未动）：**20/20 成功**，clear 7.81% / technical 3.93% / light 25.21% / medium 24.62% / heavy 29.48%，SECS 0.844–0.868（参照物是新参考音，与旧跑不可横比）；
- 旧问题句 `蓝色纸鹤…` 用旧参考音 12.5%、新声音 0.00%：可懂度问题主要在旧参考音；
- `clear_02 请把窗户关上…` 新参考音 25%（丢尾+请→先）、旧参考音 0%——**参考音相关、引擎无关**，低电平参考音会让句尾收急；记录在案，按用户指示不在录音质量上循环；
- 结论：`clear/technical ≤5%` 尚未稳定过线，最大变量是参考音电平；音色相似度不再作为工作项。

---

## 0c. 2026-10-25 声线资产契约落地（P1.4 完成）

按 `docs/ARCHITECTURE.md` §3.2–3.4 实现 `backend/services/voice_assets.py`：

- **VoiceCard**：`.guangpu_webui/voice_cards/<voice_id>/card.json`，版本 `date.N` **只增不改**；timbre（ref_audio/ref_text/底模/prompt_lang）与 defaults（lexicon/accent_level/intensity/style_id）分离；`license_id/consent_id` 字段就位（授权账本接线时用）；
- **StylePack**：`assets/style_packs.json`（natural/calm/energetic/narrative），映射 `speed/pause_second`；
- **Accent**：复用 `pronunciation.py` 四档，新增公开 `accent_intensity_for()`；
- API：`/api|/v1` 双挂载新增 6 条路径（list/create/get/patch/versions/activate）；`POST /synthesis` 接受可选 `voice_card_id`/`style_id`，优先级 **请求显式 > 卡默认 > 保存配置**；`retired` 卡拒绝新合成（撤销语义）；`timbre_available=false` 只提示不拦截；
- 真实冒烟：从用户当前配置存卡 `vc_20261025_125337_d8c8d6 v2026-10-25.1`，带卡 + calm 风格真实合成成功；
- 同日把参考音门禁的"拦截感"改为提示性：`isReferenceBlocking`→`referenceQualityFailed`，"判定为不可用"→"已填入，可直接生成"，报告 fail 标签"不可用"→"未达标"——门禁从未在后端拦截，此前只是文案吓人；
- **2026-10-25 晚，用户把 webui-rebuild 新前端合并进主工作区**（旧 App.vue/IconRail/TimbrePanel/threeScene 删除，新结构 `views/composables/styles/lib`，49 项测试、build 通过）。新向导 `VoiceWizardView` 原本把 fail 做成了真拦截（隐藏"确认使用"按钮），已按同一原则重放修复：质检未达标只给 `--warn` 提示，确认按钮常显，向导可继续。

---

## 0d. 2026-10-25 交换测试纳入固定回归（ARCHITECTURE §12 条目 4 完成）

- `scripts/tts_regression.py` 内置同文本四档口音交换套件（`SWAP_BASE_TEXT`/`SWAP_LEVELS`/`run_swap_suite`/`summarize_swap`）：默认随 20 句一起跑（合成到 `<out>/swap/<level>/`），`--no-swap` 跳过；评测复用同一次加载的 SenseVoice/ERes2NetV2，不再双份加载；
- 报告新增 `swap` 节：逐档 CER/SECS、`cer_rises_with_accent`、`secs_spread`、`failed_levels`——描述性汇总，不做 pass/fail 门（SECS 单独不能验收）；
- `accent_swap_verify.py` 改为委托 `run_swap_suite`（常量同源 `assertIs` 锁死），CLI 与旧报告结构兼容、新增 `summary` 字段；
- **换音色交换测试就绪**：`--voice-card ID`（可重复）让整套 20 句 + 交换按 VoiceCard 跑（`voice_assets.apply_voice_card`），两张卡同跑即"换音色不换发音"；报告 `voice_runs` 节按卡分组，输出落 `<out>/voices/<id>/`；
- 真实验证：卡片模式冒烟（`output/tts_regression_card_smoke/report.json`）clear_01 CER 6.25% 与直连配置一致；交换套件 CER off→heavy 0.00→20.69%、SECS 0.85±0.03、`目的/但是` 四档未破坏；
- 全量 20 句 + 交换的本人声音固定回归报告：`output/tts_regression_myvoice_full/report.json`。

---

## 0e. 2026-10-25 收尾三件事

1. **seed 确定性核验（已知问题 #7 解决）**：`core` 实际已读 `cfg['seed']`（每行 line_seed、重试 `base_seed+attempt*104729`、引擎请求带 seed，`test_synthesis_reverse` 有契约测试）。硬证据：同配置两次全量回归（间隔数小时）输出 **sha256 字节级一致（抽样 11/11）**。回归报告 `settings.seed.effective` 改为 True。
2. **换信道交换测试工具**（ARCHITECTURE §7 #4）：新增 `scripts/channel_verify.py` + `backend/tests/test_channel_verify.py`——每段录音自动走 `voice_ingest`（切片/门禁/ASR，无需人工转写），参考两两 SECS 矩阵 + 每信道各合成一句固定文本并对全部参考算 SECS。首轮真实数据（新手机录音 vs 旧备份切片）：参考两两 SECS 0.632；输出对自己参考 0.849/0.778、对对方 0.519/0.587——**零样本音色确实携带信道/会话特征**，量化了 §4.1"信道/噪声增强"的必要性。注意：这两段还差了会话与朗读风格，不是纯信道对照；干净对照需同天双麦克风录音。报告 `output/channel_verify/report.json`。
3. **前端声线卡闭环完成（前端侧并行完成）**：新前端 `lib/api.ts` 的 VoiceCard 契约与后端逐字段对齐；向导 `finish()` 修为 `createVoiceCard`（此前调用的 `saveAlias` 不存在，会在最后一步崩溃）；声音库页已有列表/激活。`patchVoiceCard`（停用/恢复）API 客户端与 UI 尚未加。

**P1 剩余**：音色 adapter 路线决策与实现（需数据）；换音色交换测试待第二张卡；B2B（P2）按红线等 CER 过线。

---

## 0f. 2026-10-25 换音色交换测试 + adapter 路线决策（用户拍板）

**用户决策（提问卡确认）**：

1. **阶段 2 走「零样本优先」**：不训练；改进参考音挑选、信道/噪声增强、多参考音库；等换音色/换信道数据齐再议训练。
2. **任何未来训练必须以 5–30 秒短音频为前提**（用户原话"依然保留 5-30 短训练音频的前提"）——不采用需要 3–10 分钟录音的 standard_voice 假设；`quick_clone` 形态是产品约束。

**换音色不换发音交换测试**（首跑，`output/tts_regression_exchange/report.json`）：用户提供一段游戏角色语音视频（胡桃·永劫无间台词纯享，33.7s），ffmpeg 抽音轨 → `voice_ingest` 自动建档（10 候选中选 6.82s、门禁 ok、ASR 正确）→ 建临时诊断卡 → `tts_regression.py --voice-card A --voice-card B --group clear --group technical`（两卡同跑 = 交换测试 #1）：

| 组 | 我的声线 | 胡桃参考（诊断） |
|---|---:|---:|
| clear | 7.81% | 26.56% |
| technical | 3.93% | 0.00% |
| 剔除各自病理句后 clear | ≈2.1% | ≈2.1% |

结论：**剔除病理句后两音色 CER 几乎一致（发音与音色解耦成立）**；但每个参考音各有自己的脆弱句——我的声线 clear_02 丢尾（25%），胡桃参考 clear_03 **合成塌缩**（0.64 秒、CER 100%、SECS 0.42，已被 suspiciously_short 标记）。零样本路线的下一个工程杠杆：**参考相关的生成稳定性**（输出可疑短/CER 灾难时换种子重试，`_synthesize_seeded` 现仅按采样率重试）。胡桃参考的四档交换 SECS spread 0.24（我的 0.026），戏剧化演绎参考音的说话人相似度更飘。

**合规记录**：胡桃素材为无授权商业游戏语音，仅用于本地诊断交换测试；临时卡已删除，未进训练/未留声线卡资产；`TEMP/hutao_test/` 与 `output/tts_regression_exchange/` 为诊断产物（gitignored）。此路径不得用于产出对外内容。

---

## 0g. 2026-10-25 读回自选 take（零样本路线可靠性杠杆，已上线产品路径）

**机制**（`core._synthesize_lines_locked` 行级）：zh 单语行走本地 SenseVoice 读回（判 WAV），CER > 15%（灾难阈值，故意高于 5% 验收线——只救灾难不刷分）或可疑短 → 用 `line_seed + take×7×104729` 派生种子补 take（首 take 永远用原始 cfg，好 take 字节级不变）；最多 3 take，全坏取读回最优；take 判定在 GPU 租约内。读回模型不可用→静默保留首 take。非中文/显式混语行不参与。`take_selection` 配置默认开（env `TAKE_SELECTION=0` 关），`POST /v1/synthesis` 可带该字段；输出带 `takes/selected_take/take_cers`，回归 sidecar 透传。GPU 租约契约测试补了 no-op judge stub（读回不得在租约契约测试里加载真模型）。

**验收**（`output/tts_regression_myvoice_takefix/report.json`，20/20、0 削波、0 可疑短、交换套件与改前逐位一致=确定性保持）：

| 组 | 改前 | 改后 |
|---|---:|---:|
| clear | 7.81% | **5.73%** |
| technical | 3.93% | 3.93% |
| light | 25.21% | **16.76%** |
| medium / heavy | 24.62% / 29.48% | 24.62% / 29.48% |

- clear_02 三个 take 全部 ≥16.67%（25/25/16.67）——**参考音能力上限，非坏运气**；5% 线（当前 5.73%）被这一句卡住，根因仍是低电平参考音（用户已决定不重录）；
- light 组四句全部重试后改善（如 20→35→15 取 15%）；medium/heavy 各 take 一致地差，重试正确地没有"改善"它们；
- 已知偏差：行内判定读 WAV、回归判 MP3，编解码会带来轻微判定差（clear_01 行内 0% / 回归 6.25%），属可接受噪声；
- 回归语义变化：bench CER 现在度量"交付后"质量（含选 take），阈值 15% 高于验收线 5%，不构成自我实现的刷分。

---

## 0h. 2026-10-25 市场调研与下一期功能决策（用户拍板）

**调研背景**：全项目实测评审 + 竞品调研后用提问卡确认方向。关键信号：Replica Studios（游戏声音 AI 先发者）2025-06-30 关站留出空位；ElevenLabs Iconic Voice Marketplace + Voice Library 分成验证了"声线=可授权资产"；开源表现力天花板已是 IndexTTS2/2.5（B 站，情感与音色解耦 + 精确时长控制），GPT-SoVITS 在"表现力"心智上落后；短剧译制/配音工具为红海，不碰全管线；国内 C 端价格被打穿，只能按角色/项目授权定价。

**用户决策（提问卡确认）**：

1. **P0 三项全做**：整集批量生产 + SRT 时间戳；多角色对话编排（角色绑声线卡 → 逐句合成 → 整集/分轨导出）；情感/表现力控制。
2. **情感技术路线**：评估接入 IndexTTS2 为第二引擎（与四层分离同哲学），先出「接入 vs 自研」评估结论再定，不在 GPT-SoVITS 上硬追。
3. **P1 本轮包含**：发音词典用户入口（多音字/专名读音修正，把 PronunciationPlan 积累产品化）；输出水印 + C2PA 溯源（license_id/consent_id 从字段变可验证资产）。
4. **商业验证路径**：先补 B2B 平台（API Key/配额/租户/持久化队列；2026-10-25 已落的 RequestGuard + XIANGHUA_API_KEY 是第一步），平台扎实后再找短剧团队试点。

**建议执行顺序**：批量+SRT（纯产品化、最快）→ 对话编排（依赖批量）→ IndexTTS2 评估（并行启动，两周出结论）→ 发音词典入口 → 水印+C2PA → 租户/配额/持久化队列。

## 0i. 2026-10-25 晚：0h 决策当日落地（除 IndexTTS2 评估外全部上线）

按 0h 决策当日完成 P0 两项 + P1 两项 + B2B 第一阶段，全量回归后端 **493 passed / 1 skipped**、前端 **50 passed**、build 通过：

- **整集交付**（P0-2）：`backend/services/batch_production.py`——SRT 累计时间轴（`【角色】`前缀）、ffmpeg concat 整集音轨、按角色分轨；`POST /batch/export`（任务完成后可导出）；工作台出现「导出整集」按钮。
- **多角色对话编排**（P0-3）：`backend/services/dialogue.py`——`名字：台词` 剧本解析（无冒号归旁白）、角色绑声线卡逐句合成、单句失败（含卡不存在）不中断；`POST /dialogue/synthesis|export`、`GET /dialogue/status/{id}`；导出含每角色分轨。
- **发音词典用户入口**（P1-1）：`backend/services/user_lexicon.py`（`.guangpu_webui/lexicon.json`，上限 500 条）；`build_pronunciation_plan` 新增 `user_overrides` 参数——**用户词典在所有口音档位（含 off）都生效，优先级高于口音规则与固定搭配保护**，replacements 带 `origin: user_lexicon|accent` 便于审计；设置弹窗内增删；只改读音不改字幕。
- **溯源凭据**（P1-2）：`backend/services/provenance.py`——每条输出写 `<stem>.provenance.json`（HMAC-SHA256 签名，含 file/text sha256、license_id/consent_id 从声线卡自动带出、卡版本）；`POST /provenance/verify` 校验文件+签名；`XIANGHUA_PROVENANCE=0` 可关；测试经 conftest 全局关闭防污染真实配置。
- **B2B 第一阶段**：`api_keys.py` 多租户注册表（token 只显一次、sha256 落盘、日配额+RPM 滑窗、chars 计量，`/api/api-keys` 管理，`XIANGHUA_API_KEYS_FILE` 启用，`/v1` 按 Key 认证+限流）；`job_store.py` + `synthesis_jobs.enable_persistence()` 合成快照落盘 `.guangpu_webui/jobs/`，重启恢复历史任务（中断标记失败）——已知问题 #8 闭合。
- **测试**：新增 `test_security_guards`（22）、`test_platform_features`（13）、`test_batch_production`+`test_dialogue`（19）、`test_user_lexicon`（含发音层 12）、`test_provenance`/`test_api_keys`/`test_job_store`（23）；子 Agent 并发限制两次失败后批量/对话服务由 Agent 交付代码、测试由主会话补齐。
- **IndexTTS2 评估完成**：`docs/INDEX_TTS2_EVALUATION.md`（Agent 三次死于并发限制，主会话自写）。核心结论：**有条件接入**——技术强契合（情感/时长解耦），但 bilibili 自定义许可证**商用需书面授权**（issue #228），授权前只进实验分支不进产出路径；建议 `emotion/duration_hint` 字段先落 API 契约，备选 Apache-2.0 的 CosyVoice-instruct。
- **未完成**：对话编排的专用前端 UI（API 已就绪，工作台只挂了整集导出按钮）；情感控制实现本体待授权与实验结论。

## 0j. 2026-11-01 全量审查与架构重构（本轮）

**入口文档**：`docs/REFACTOR_2026-11.md`（缺陷清单、分层说明、验证结果、未做项）。

- 后端缺陷修复 13 项，其中两条会直接丢数据/挂死：任务 ID 毫秒碰撞（同毫秒两个任务互相覆盖）、
  `save_audio` 内 ffmpeg 无超时且运行在 GPU 租约内（卡住则租约永不释放，之后所有合成报「GPU 占用」）。
  `history.json` / `config.json` / 媒体会话 JSON 的读改写全部改为带锁 + 原子写 + 损坏隔离。
- 新增分层：`backend/infra/`（atomic / jsonstore / logging / ids）、`backend/domain/text.py`、
  `backend/api/{deps.py,routers/*}`。`app.py` 1431 → 178 行，76 条路由进 8 个模块；
  `core.py` 1620 → 1391 行（纯文本层 528 行外移，`core` 保留同名再导出）。
- 新增 `config_guard`：`PUT /config` 与合成请求体不能再把 `ref_audio`/`gpt_model`/`gpt_sovits_root`
  指向任意位置（此前可读任意音频、可 `torch.load`、可把任意目录塞进 `sys.path`）。
- 工程化：`requirements.txt` + `requirements.lock.txt`(240) + `requirements-ci.txt`、
  `pyproject.toml`（ruff 固定规则集 + pytest 配置）、`.github/workflows/ci.yml`；
  全项目从 0 处 logging 变为接入结构化日志；`on_event` 改 `lifespan`（告警 113 → 1）。
- 磁盘保留：`services/retention.py` + `GET /api/maintenance/retention`（只报告）/
  `POST /api/maintenance/prune`（默认 dry-run）；启动时 `JobStore.purge(keep=200)`。
- 前端修 13 项，含：迟到状态响应劫持当前任务、逐行音频按下标错位、生成按钮生成完即锁死、
  loss 曲线 `MM…` 非法路径、API 层丢状态无超时、关标签页丢防抖保存。
- 验证：后端 505 passed / 1 skipped、ruff 全绿；前端 50 passed、`vue-tsc` 与 `vite build` 通过。
  测试数与重构前一致，说明是等价重构。
- 第一轮遗留项已按用户逐条定夺在**第二轮**完成（详见 `docs/REFACTOR_2026-11.md` §7）：
  - 补修三项误标为「已修」的缺陷：scan 静默改根（现改只读）、导出声称成功（现 strict 报 500）、
    读回降级原因不外露（现 `readback` 字段 + 300 秒自动重试）；
  - **任务取消**：合成逐行、对话行间、音色两个边界；`JobRegistry.cancel` 按 kind 分发；
    对话任务原先根本没进 `/api/jobs`，本轮补上 `KIND_DIALOGUE`；新增 `test_cancellation.py`（10 项）；
  - **core.py 全拆**：`domain/text.py`(529) + `engine/tts.py`(317) + `pipeline/synthesis.py`(571)，
    core 1620 → 777 行；pipeline 不 import core，靠 `SynthesisHooks` 注入，测试接缝全部保留；
  - **前端轮询收敛**：新增 `usePolling`（无重叠、失败重试、generation 防旧响应），
    迁移 WorkshopView / VoicesView 三处"瞬时失败即永久停表"；
  - **媒体上传后台化**：请求只落盘即返回 `uploading`，转码线程完成后自动接续分析；
  - **覆盖率基线 79%**（pytest-cov，只记录不设门槛，CI 上传 XML）；
  - `docs/HANDOFF.md` 顶部标记过期并指向新文档。
- 验证（第三轮结束时的最新数字）：后端 **543 passed / 4 skipped**、ruff 全绿、覆盖率 **80%**；
  前端 **57 passed**、`vue-tsc` 与 build 通过；GPU 冒烟（`XIANGHUA_GPU_TESTS=1`）真实推理通过。
- **第三轮（同日）把上述清单逐条做完**：
  - 音色推理可中断：`knnvc_runtime` 在特征分块/参考文件/匹配前三处检查，`TimbreCancelled` → `cancelled`；
    唯一仍不可中断的是一次 `model.match` 矩阵运算；
  - 顺带修复：音色 worker 的收尾 `_update` 在 try 之外且用 `result['键']` 取值，任何收尾异常都会让任务
    永久停在 `running`（前端无限轮询），现已加 `.get()` 兜底 + 外层终态保证；
  - `timbre.py` 覆盖率 43% → **61%**（新增 `test_timbre_backend_flow.py` 11 项），总覆盖率 **80%**；
  - 前端轮询**全部收敛**：`useJobs` / `useSynthesis`（状态 + 预热）/ `useReferenceAudio` / `useTraining`
    全部改用 `usePolling`（新增函数式 interval），并删除 `timbre.ts` 里 70 行从未被调用的 `pollTimbreJob`；
    现在 `frontend/src` 中已无任何手写 `setInterval`；
  - `useTraining` 的 `ref<any>` 清零（`lib/api.ts` 新增 7 个训练类型），`EventSourceLike` 处理器参数也收窄；
  - **GPU 自动化测试**：`backend/tests/test_gpu_smoke.py`（默认跳过，`XIANGHUA_GPU_TESTS=1` 开启），
    本机实测通过（含模型加载 28 秒）——这同时验证了 engine/pipeline 拆分没有破坏真实推理；
  - **确定性边界修正**：跨进程同 seed 字节一致（两次实测 `6cdaf6af…`），但**同进程内重复调用不一致**；
    原「字节级一致」结论只在每次全新进程时成立，已写入 §5 第 7 条并新增
    `scripts/seed_determinism_check.py` 作为可执行校验。
- 仍未做（下一轮候选）：JSON 未换 SQLite（用户既有决定）；`output/` 与媒体会话无自动清理定时器；
  `doctor.py` 覆盖率 21%；合成页 SSE 与 500ms 轮询并存（共用 revision 守卫，不会重复应用）。

## 0k. 2026-11-13 磁盘瘦身与工程结构收敛（本轮）

**磁盘瘦身（共回收约 5.1GB，均经用户确认）**：

- 删除 `TEMP/` 全部 5.0GB（含 `acceptance_hutao` 4.0GB 验收产物与 `handover_cleanup` 0.9GB 旧训练资产唯一备份——**旧切片/旧权重备份已不可还原**，用户拍板删除）；
- 删除根目录 `GPT_SoVITS/` 空壳（仅 1 个旧 `tts_infer.yaml`，后端引擎 cwd 恒为 `GPT-SoVITS/` 连字符目录、配置程序化传参，零引用）、`.coverage`、`.playwright-cli/`、`.ruff_cache/`、`.zcode/`、frontend 下杂项；`.pytest_cache/` 因 ACL 锁死无法删除（见下"遗留"）；
- 删除过期文档 `docs/MVP_VALIDATION.md`、`CORE_ARCHITECTURE_AUDIT.md`、`MEDIA_INGEST_HANDOVER.md`、`docs/superpowers/` 与被否设计稿 `design/A-bright.html`、`B-dark.html`（git 历史仍可找回）；
- `frontend/public/logo.png` 1024px 1.16MB → 256px 84KB（仅作 favicon 用）。

**结构收敛（全部"新家 + 旧位垫片"，行为保持）**：

- 训练后端收编：`tools/train.py`(897) → `backend/services/training.py`，`tools/env.py`(96) → `backend/infra/gptsovits_env.py`；旧位留 re-export 垫片，backend 内 import 全部直连，消除 backend↔tools 双向耦合；
- GPU 租约迁 infra：`services/gpu_arbiter.py` → `backend/infra/gpu.py`（`GPU_ARBITER` 单例同一），engine/pipeline 不再依赖 services；
- 路径常量下沉：新建 `backend/infra/paths.py`（16 个纯常量），path_guard/provenance/user_lexicon/voice_assets/voice_selection 改直连；`SUPPORTED_SYNTHESIS_LANGUAGES` 直接取自 `backend/domain/text.py`（避免双源）；retention.py 因还用 core.load_history 且测试 patch core 属性，有意保留经 core 读路径；
- 死代码：删 `tools/_check_batch.py`（0 引用）；`tools/local_tts.py` → `scripts/local_tts.py`；6 个实验期脚本归位 `scripts/experiments/`（five_language_verify/f0_estimate/sing_convert/speaker_embedding/asr_readback/smoke_timbre_api），相关 sys.path 自举与测试同步更新；
- 测试改名：`test_mixed_language_quality_experiment.py` → `test_mixed_language_baseline_experiment.py`（对齐被测模块）；
- 前端：删死组件 `LanguageMarkerInput.vue`(225 行全仓零引用)、base.css 死选择器 `.qc-row` 与错层 `.wb .inspector`（迁回 WorkbenchView scoped）；
- 文档失实修正：README/ARCHITECTURE/USAGE_GUIDE/WEBUI_GUIDE 的 kNN-VC"在用"表述改为"已停用"；README 测试数字 543+57；setup.ps1/setup.sh 统一 venv 到仓库根 `.venv`（消除与 README 矛盾）、修 setup.sh 相对路径检查 bug、改题"像话 XiangHua"；patches/README 旧父目录路径修正；B2B 草案顶部加现行架构指向；`REFACTOR_2026-11.md` 顶部状态行改最终数字；
- 解决工作区 stash 遗留：`.gitignore` 的 `UU` 冲突按双方取超集解决，结果与 HEAD 完全一致，git 状态恢复全绿。

**验证**：后端 `pytest -q` **546 passed / 4 skipped**（+44 subtests；skips=GPU 3 + g2p 数据缺失 1，与基线一致）、`ruff check backend tools scripts` 全绿；前端 **58 passed**（+1，死组件删除后测试全过）、`vue-tsc` 与 `vite build` 通过（CSS 32.4KB）。

**遗留（下一轮候选）**：根目录 `.pytest_cache/` 因 Windows ACL 锁死无法删除——工作区属主是另一 Windows 账户（SID 后缀 -1001，当前进程用户 -1008），需用户手动 `takeown` 后删除或以资源管理器删除；`docs/HANDOFF.md` 旧路径文案未更新（顶部已标过期指向 REFACTOR）；`media_ingest(1508)/reference_audio(1005)/timbre(730)/job_registry(707)/core(777)` 五个大文件拆分未做；`WorkbenchView.vue`(551) 拆分未做；`lib/api.ts`(374) 按域拆分未做；`output/` 约 250 个历史合成目录可用 `POST /api/maintenance/prune` 清理（未动）。

## 0l. 2026-11-13 结构整理第二阶段：大文件拆分与分层归位（0k 遗留清单执行）

同日接续 0k，用子 agent 分三波完成 0k 遗留的全部结构项，**全程行为保持（新家 + 旧位垫片），三轮全量验证零漂移**。

**第一波**：
- `media_ingest.py` 1508 → **56 行门面**：拆出 `media_sessions.py`（684，会话存储/上传/ffmpeg 抽轨）与 `media_analysis.py`（732，VAD/聚类/切片/转写/导出激活，`MediaAnalysisMixin`）；3 处 patch 缝同步改测试到新家，其余经门面保真（同一对象已验证）。
- `reference_audio.py` 1005 → **660 行**：拆出 `reference_audio_metrics.py`（282，测量原语层）；原文件保留 DSP 门禁判定（实际内容是纯门禁，同步上传/自动建档本就在 voice_ingest/voice_prepare_jobs）。
- 前端 `lib/api.ts` 374 → **18 行 re-export 入口**：拆出 `lib/api/` 下 types.ts(314)/client.ts(95) + 按域端点模块（reference/voiceCards/jobs/history/lexicon/dialogue/export/provenance）；ReferenceReport 类型族从 composables 上移到 lib/api/types，**lib→composables 反向依赖消除**。
- 前端 `WorkbenchView.vue` 551 → **381 行**：声音检查器整块抽成 `components/workbench/InspectorPanel.vue`（215 行，样式与文案逐字不变，五个单例 composable 组件内复用）。

**第二波**：
- `job_registry.py` 707 → **165 行门面**：拆出 common(184)/synthesis(154)/timbre(105)/training(137)/media(76) 五个按 kind 的收集器文件；收集器保持 lazy import 读模块属性模式，全部 patch 缝天然保真，test_job_registry.py 零改动。
- `timbre.py` 730 → **70 行门面**：拆出 timbre_refs(165)/timbre_features(142)/timbre_worker(462)；**顺手消除 timbre.py 的 sys.path hack**——`knnvc_runtime` 改为 `importlib.util.spec_from_file_location` 按路径加载并缓存（scripts/knnvc_runtime.py 原地不动）；仅 1 处 patch 缝改到真模块（test_timbre_backend_flow 的 reference_files → timbre_features）。

**第三波**：
- **domain 倒置环修复**：`pronunciation.py`(204)、`user_lexicon.py`(121) 搬入 `backend/domain/`，原位留 15/18 行垫片；text.py/pipeline/core/voice_assets/routers 改直连；**grep 确认 domain 包内零 `backend.services` import**。1 处字符串 patch 改到真模块（test_platform_features → backend.domain.user_lexicon.lexicon_path）。
- **core.py 767 → 365 行**：配置读写 → `config_store.py`(102)、history/downloads → `history_store.py`(149)、environment_check/training_stats → `doctor.py`（并解除 doctor 对 core 的依赖防环）。`patch.object(core, ...)` 缝逐条核验：因 core 保留全量 re-export 且 hooks 调用时从 core 命名空间现读，仅 1 处（CONFIG_FILE patch）改到 config_store；`hasattr(core,"_CONFIG_LOCK")` 契约保留。
- **app.py 188 → 146 行**：兼容 re-export 块删除（真身早已在 deps.py），4 处测试 import 迁到 `backend.api.deps`，全仓零 `backend.app._*` 残留。

**最终数字**：后端 `pytest -q` **546 passed / 4 skipped（+44 subtests）**与 0k 基线逐位一致（skip=GPU 3 + g2p 数据 1）、`ruff` 全绿；前端 **58 passed**、`vue-tsc` 与 `vite build` 通过（178.2KB JS / 32.5KB CSS，与基线持平）。本轮净新增约 30 个文件（新模块 + 垫片），全仓无超过 800 行的 Python 文件（scripts/tts_regression.py 1114 行除外，属回归台架 CLI）。

**新遗留（下一轮候选）**：`_CONFIG_LOCK/_DOWNLOADS_LOCK` 在 core 已无实际使用者（真锁在 jsonstore），仅为 hasattr 兼容断言保留；`timbre_worker.py` 462 行为最内聚粒度不再拆；8 个测 backend 之外代码的测试文件仍在 backend/tests（迁移需同步 pyproject/CI，未做）；`output/` 历史目录清理仍可用 `POST /api/maintenance/prune`。

---

## 0. 2026-10-17 清场记录

已删除：

- 过期研究文档、旧实现计划、SVC/RVC/唱歌死路记录；
- `archive/`、`experiments/`、`output/`、根目录旧 `tests/`、`preview/`；
- `.cache/`、`.pytest_cache/`、`.playwright-cli/`、`.superpowers/`、`.webui/`、旧知识图谱；
- `third_party/DiaMoE-TTS/`（约 29.5GB）及其专属脚本/测试/资产；
- 旧训练特征 `GPT-SoVITS/custom_data/dataset/`；
- 旧中间 checkpoint（保留每组最终权重）；
- `.guangpu_webui` 的历史、下载记录、训练日志和旧运行日志。

保留：

- `GPT-SoVITS/custom_data/raw/`、`sliced/`、`asr/`：用户原始录音、切片、转写，不能删；
- `GPT-SoVITS/GPT_weights_v2Pro/`、`SoVITS_weights_v2Pro/` 的最终权重；
- `GPT-SoVITS` 引擎、底模、辅助模型；
- `.venv/`、`frontend/node_modules/`、`.guangpu_webui/models/`、`reference_audio/`；
- `third_party/knn-vc/`：当前音色转换链路仍在用；**（2026-11-13 更正：该目录实际已不存在，kNN-VC 音色转换链路处于停用状态，需要时按上游仓库克隆恢复；`scripts/knnvc_runtime.py` 与 `backend/services/timbre.py` 代码保留。）**
- `.worktrees/`：两个旧 worktree 仍有未提交改动，未删除，需用户确认后再处理。**（2026-11-13 更正：`git worktree list` 现只剩主工作区，旧 worktree 已不在。）**

清场未提交，也未 push。

---

## 1. 项目方向

当前主线不是“又一个通用 TTS”，而是：

> **面向独立游戏与短剧团队的“角色声线 API + 训练能力 + 授权账本”。**

产品顺序：

1. 短剧/剧情内容团队先验证付费；
2. 独立游戏团队做深 Unity/Unreal、私有化和按角色授权；
3. 训练能力分三层：Managed Training → Training API → On-prem；
4. 每条输出带 `license_id`、`voice_version`、manifest 和可验证授权记录。

完整商业策略见 `docs/B2B_VOICE_API_STRATEGY.md`。

---

## 2. 新架构硬约束

必须把以下四层分开：

| 层 | 只负责 | 数据来源 |
|---|---|---|
| 音色层 | 说话人身份/音色 | 目标音频，仅用于 speaker encoder/adapter |
| 发音层 | 字→音素、多音字、口音读音 | 发音字典 + G2P，与目标音频无关 |
| 韵律层 | 停顿、重音、语调、情绪 | 多说话人标准语料/风格标签 |
| 口音层 | 音素替换、语调节奏 | 独立 accent pack，不进入音色模型 |

目标音频**不能**再用于全量 fine-tune 去学习口音、韵律和内容。
当前 GPT-SoVITS 全量微调会把这些因素纠缠在一起，是“SECS 很高但听不清”的根因。

详细数据契约、评估与迁移路线见 `docs/ARCHITECTURE.md`。

### 2.1 输入原则（2026-10-17 修正）

产品输入必须是**一段正常的自然说话音频**，不是用户照着台词本补录的训练语料。

- 用户只需上传已有录音：访谈、播客、会议、语音备忘、已有配音等（必须有权使用）；
- 系统自动完成 VAD、切片、ASR、质量门禁、参考片段选择和音色资产建档；
- 用户不需要逐字填写参考文本，不需要标注发音，不需要补录特定口音；
- 自动识别有错字时才需要可选修正，不能把“人工校对”变成默认前置负担；
- 发音由字典/G2P 决定，韵律和口音由底座与 accent pack 决定，**不从目标音频里补采**。

---

## 3. 当前代码事实

- 后端：FastAPI，入口 `backend/app.py`；当前是本地单用户 API，不是多租户 B2B API。
- 前端：Vue 3 + Vite，入口 `frontend/src/App.vue`；页面为合成、训练、采集、历史、设置、音色。合成页为单屏单列：状态条 + 台词区 + 内联结果 + 底部一个主操作；音色和参数收进自动保存的抽屉，不再有双主题、新手引导、结果弹窗/托盘或右侧常驻栏。
- 引擎：GPT-SoVITS v2Pro；当前默认配置已切到“清晰优先”：
  - `gpt_model = GPT_SoVITS/pretrained_models/s1v3.ckpt`
  - `sovits_model = GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth`
  - `ref_audio = custom_data/sliced/训练.wav_0000018880_0000143680.wav`
  - `ref_text = 今天天气真不错，我们出去走走吧。`
  - `accent_level = off`
  - `auto_lightmid = false`（旧兼容开关，新调用不再使用）
- 发音层已档位化：`backend/services/pronunciation.py` 提供 `off / light / medium / heavy`、固定搭配保护与 `PronunciationPlan`；`tools/rules.py` 仅保留旧入口。
- 参考音配对：`backend/services/reference_prompt.py` 读取 ASR 清单并校验参考文本；WebUI 自动带出已知切片原文，明显不匹配会在合成前拒绝。
- 单音频自动建档：`backend/services/voice_ingest.py` 从一段自然说话录音中自动选 2–10 秒片段、跑质量门禁并 ASR；`POST /api/reference-audio/prepare` 走异步 job（`backend/services/voice_prepare_jobs.py`），切片/质检/ASR 在后台线程跑，前端按阶段轮询，长录音不再阻塞 HTTP；同步 `POST /api/reference-audio` 保留兼容。
- 发音计划持久化：`core.synthesize_lines` 每句 output 带 `pronunciation`（完整 `PronunciationPlan.to_dict()`），并在输出目录写 `pronunciation.json`；manifest 写失败只记 `pronunciation_manifest_error`，不影响合成。
- GPU 租约：`backend/services/gpu_arbiter.py`，合成/模型加载/媒体分析/单音频建档/音色转换/训练共用一把进程级 FIFO 租约；`/api/overview` 暴露 `gpu` 状态，忙满 60 秒抛中文 `GpuBusyTimeout`（HTTP 409 `GPU_BUSY`）。
- 任务注册表：`backend/services/job_registry.py` 聚合五套任务状态，`GET /api/jobs`、`GET /api/jobs/{id}`、`POST /api/jobs/{id}/cancel`（仅训练可取消）。
- 数据集版本化：媒体分析写 `segments/<run_id>/` 且只增不删；`export_dataset(run_id=)` 导出历史 run；`activate_dataset(dataset_id=)` 重放历史数据集。
- API 版本化：`/api`（内部 WebUI）与 `/v1`（对外契约）由同一个 `APIRouter` 双挂载，55 条路径完全对齐；错误响应统一 `{"detail", "code"}`；未知 API 路径返回 JSON 404。契约见 `docs/API_V1.md`。
- 许可证：`LICENSE` 为 AGPL-3.0。
- 回归台架：`scripts/tts_regression.py` + `backend/tests/fixtures/tts_regression_20.json`；报告写入 `output/tts_regression/report.json`。
- 训练管线仍在：`scripts/run_dataset_prep.py`、`scripts/run_training_chain.py`、`scripts/verify_training_loop.py`、`tools/train.py`。
- 声音档案/口音包抽象仍在：`backend/services/voice_selection.py`、`assets/accent_packs.json`；口音包资产尚未补齐。
- kNN-VC 语音音色转换链路保留：`scripts/knnvc_runtime.py`、`backend/services/timbre.py`、`third_party/knn-vc/`。**（2026-11-13 更正：`third_party/knn-vc/` 已不存在，链路停用；代码侧 `backend/infra/gpu.py` 等见 0k。）**

---

## 4. 验证状态（2026-10-17 首轮 MVP）

- 后端 unittest：**303 tests，0 failures，0 errors**（含发音、配对、回归、自动建档、异步 job、媒体采集、GPU 租约、数据集版本化、任务注册表与 API 双挂载测试）。
- 前端：**124 tests 全过**；`npm run build` 成功。合成页布局在 1440×900、1024×768、390×844 下均不产生页面级滚动；音色/参数抽屉支持遮罩点击与 Esc 关闭，配置改动 600ms 防抖自动保存。
- `create_app()` 可导入并正常注册路由；含 `/api/reference-audio/prompt` 配对查询与 `/api/reference-audio/prepare` 异步建档。路由总数会随并行开发变化，不作为验收常量。
- 20 句真实回归：**20/20 成功**，0 削波、0 可疑短音频。
  - clear CER 3.12%，technical CER 0.00%；
  - light / medium / heavy CER 分别为 16.09% / 22.46% / 31.18%；
  - SECS 全程 0.78–0.81，说明“更像”不等于“更清楚”；
  - 同文本四档交换：off/light/medium/heavy CER 3.33%/3.45%/13.79%/20.69%，SECS 0.805/0.801/0.779/0.795；
  - 训练外叙述句出现 `纸鹤→紫鹤`、`雨后→以后`，该句 CER 12.5%。
- 单音频自动建档：3.9 秒自然片段和 5 分 44 秒原始录音均自动选出参考片段并完成 ASR；异步接口 `POST /api/reference-audio/prepare` 实测 7 秒完成，返回 job 快照与 `ref_audio + ref_text`。
- 发音清单：真实合成一次写盘 `pronunciation.json`，`lexicon_id=lex_zh_base_v1`、`accent_pack_id=guangpu_medium`、11 个受保护搭配。
- 核心架构：自检 8 问 6 项变绿（并发 GPU、重跑分析、历史数据集、任务可见性等）；真实合成期间 `/api/overview.gpu` 能显示持有者，任务结束后回到空闲；详见 `docs/CORE_ARCHITECTURE_AUDIT.md`。
- 详细记录：`docs/MVP_VALIDATION.md`。

---

## 5. 已确认的问题

1. 全局“的/是”替换已从默认路径移除；但 `heavy` 档仍会做全局替换，只应在压力测试/独立 accent pack 中使用。
2. G2P 补丁仍在 `GPT_SoVITS/text/chinese2.py` 和训练特征里；改字典仍可能需要重建特征/重训，尚未彻底外置到版本化字典。
3. 旧训练集 64 条、281.7 秒，其中 52 条/226.8 秒是浓广普，清晰普通话只有 12 条/54.9 秒；模型会把浓口音学进音色。
4. 自定义音频的参考文本现在由 `voice_ingest` 自动转写，不再要求用户逐字填写；ASR 仍可能出错，因此文本保持可修正，低置信度片段需要可选复核。
5. B2B API 尚缺：API Key、租户、配额、持久化队列、项目/角色、voice version、license/consent、Webhook、水印、公开 SDK。
6. 回归只覆盖一套音色；尚未完成“换音色不换发音”“换信道不换人”等完整交换测试。
7. ~~当前 `seed` 已记录但核心尚未真正读取；复现性仍依赖核心的确定性种子。~~ **已解决（2026-10-25 核验）**：`cfg['seed']` 实际进入每行 line_seed 与重试种子派生，引擎请求携带 seed；同配置两次全量回归输出字节级一致（sha256 抽样 11/11）。回归报告的 `settings.seed.effective` 已改为 True。
   **边界补充（2026-11-01 实测）**：该结论只在「**每次全新进程**执行一次」时成立。
   同一进程内连续两次调用即使 seed 相同，输出字节也不同（引擎 warm 后存在跨调用状态）。
   验证工具：`scripts/seed_determinism_check.py`（跑 N 个独立进程比对 PCM 哈希，一致时退出码 0）。
   因此 `scripts/tts_regression.py` 这类"一个进程跑完整套"的用法是符合该保证的，
   而"进程内重试对比"不能用来证明确定性。
8. 单音频自动建档已改异步 job（进程内线程，不持久化）；后端重启或多 worker 部署会丢 job，届时前端需重新上传。这是有意保留的架构缺口：只替换 `VoicePrepareJobManager` 的存储层即可，接口与前端不用改。
9. 单音频入口按“一段自然说话音频”处理；多人素材应走 `media_ingest` 的说话人聚类与选人流程。

---

## 6. 下一步优先级

1. 用一段已有自然说话音频跑通 `auto_prepare`，确认自动切片、ASR、参考片段和授权记录。
2. 不再要求用户照读台词本或补录口音数据；缺少高质量数据时，优先寻找已授权的现有素材。
3. 训练只做音色 adapter/轻量适配，口音、韵律、发音分别由独立资产控制；重跑同一套 20 句回归。
4. 把 `PronunciationPlan` 持久化到输出 manifest，并接 `VoiceCard / AccentPack / StylePack` 数据契约。
5. 再做 Managed Training pilot、B2B API 骨架和 license ledger。
6. 只有 TTS 可懂度稳定过线后，才进入 B2B API 平台化。

---

## 7. 工作边界

- **不要替用户 commit / push**；改动保留在工作区。
- 不要执行 `git reset`、`git checkout --` 等破坏性操作。
- 不要删除 `custom_data/raw/`、`sliced/`、`asr/` 和最终权重；它们是用户资产。
- 不做无授权声线爬取；只接受本人、签约配音演员、版权方授权或明确许可的素材。
- 不把“用户照读台词本/补录特定口音”作为默认采集方式；默认输入是一段正常自然说话音频。
- 不先加功能；TTS 可懂度不过线前，不做 API 平台化。
- 需要提交时，只在最终回复列出待提交文件清单和中文 commit 摘要。

---

## 8. 2026-10-17 媒体采集能力落地

新增“采集”页与后端媒体入库管线：

- 入口：`frontend/src/components/MediaIngestPanel.vue`，导航页 `media`；
- 服务：`backend/services/media_ingest.py`、`backend/services/media_diarization.py`；
- API：`/api/media/sessions`、`/analyze`、`/select-speaker`、`/export`、`/activate`；
- 流程：上传媒体 → ffmpeg 提取 16k/32k 音轨 → VAD → ERes2NetV2 说话人聚类 → 2–10 秒切片 → QC → 可选 faster-whisper 转写 → 数据集导出；
- 训练接入：`/activate` 会把目标说话人的切片复制到 `custom_data/sliced`，写入 `custom_data/asr/sliced_corrected.list`；转写就绪时同步写 `sliced_whisper.list`，备份旧清单，并把训练步骤 `slice/asr/correct` 标记为完成；
- 边界：默认识别“谁在何时说话”，不做音源分离；BGM、重叠说话、角色变声会降低准确率；上传必须声明有权用于训练。

验证：`backend/tests/test_media_ingest.py` **22 项**测试通过；真实双说话人 SAPI 音频完成 ERes2NetV2 分离与切片；前端 125 项测试与 `npm run build` 通过。

接手后修复（详见交接文档第 20 节）：

- 分析互斥改为模块级登记表，跨请求实例也能拒绝重复分析；
- 上传校验失败会回收刚建的空会话，不再留空壳；
- 转写复用 `voice_ingest.normalize_transcript`，按会话语言做繁→简（`yue` 不转换）；
- `session.json` 原子写改为唯一临时名 + 退避重试，修掉 Windows 上偶发的 `WinError 5`。

交接文档：`docs/MEDIA_INGEST_HANDOVER.md`。

---

## 9. 2026-10-17 合成页一屏化（交互瘦身）

起因：用户反馈 WebUI “做着难受、交互很啰嗦”。实机逐页检查后确认六类冗余：音色设置同时存在于右侧常驻栏和另一主题的抽屉；新手引导 3 步、快速开始、草稿、结果托盘、结果弹窗重复同一条链路；解释性长句过多；900px 高度下底部按钮被裁；导航只有图标；设置、音色样本、合成参数各有一个“保存”。

本轮只改合成页主流程，后端接口未动：

- 单一布局，删除工作台/活页双主题、主题切换按钮与相关 CSS；旧配置的 `theme` 仍会被读取但统一按同一套界面渲染。
- 删除三步新手引导、引导 localStorage、顶部帮助入口和 `synthesis-voice-hint`；音色未设置时只在状态条给一条提示。
- 删除右侧常驻上下文栏、结果托盘、结果浮层、草稿/队列/最近完成侧栏；结果改为台词下方内联列表，可试听、下载、重试。
- 音色设置与合成参数改为右侧抽屉；抽屉内不再有保存按钮，配置改动 600ms 防抖写入 `/api/config`。
- 粒子颜色设置移到「设置 → 粒子颜色」，仍自动保存；粒子球改为纯状态展示，不再可点击。
- 修复全屏高度：`html/body/#app` 100% + `body overflow: hidden` + `.app-shell 100vh`，页面不再整体滚动，长内容在编辑区/结果区内部滚动。
- 导航图标栏未改；仍是右侧图标 + 悬停提示。

验证：前端 124 tests 全过、`npm run build` 成功；浏览器实测打字 → 生成 → 结果内联出现，语速 1.1 自动落盘，遮罩/Esc 均可关闭抽屉；1440/1024/390 三种宽度无页面级滚动。CSS 同时清掉约 900 行失效的旧主题/引导/托盘样式（78.6 kB → 58.0 kB）。

待办（本轮未做）：导航图标栏默认展开或加文字标签；训练页仍为三栏高密度信息；历史页对已清理的音频仍会渲染 `<audio>` 并产生 404。

---

## 10. 2026-10-18 WebUI 推倒重建（worktree）

用户确认“前端可以全面推倒重新构建”，并要求在独立 worktree 中进行。

- 分支：`feat/webui-rebuild`
- 路径：`.worktrees/webui-rebuild/`
- 范围：只重写 `frontend/` 与 WebUI 文档，后端接口不变；worktree 基于 HEAD 创建，因此其中的后端是旧版本，预览时使用主工作区的后端（当前以 `XIANGHUA_FRONTEND_DIST` 指向 worktree 的 `frontend/dist`，监听 7860）。
- 新结构：`views/` 六个页面、`composables/` 单例状态、`components/` 基础组件、`styles/tokens.css + base.css`；删除 Three.js、粒子球、IconRail、双主题和 1400 行 `App.vue`。
- 视觉（第二轮重置）：放弃第一版的绿色/圆角卡片语言，改为深色音频控制台（石墨底 + 琥珀信号色 + 等宽小标签 + 2–4px 直角 + 细线分隔），可切浅色；新增 `CommandBar`、`AudioPlayer`（替代原生 audio 控件），旧类名/旧 token 全部移除。
- 交互：顶部编号导航、hash 路由、KeepAlive 保留台词、配置 650ms 防抖自动保存、右侧 sheet、左下 toast（移动端顶部）、训练横向步骤条、历史按日期分组。
- 验证：50 项测试通过，`npm run build` 成功；1440/1024/390 无横向溢出；深/浅两套主题逐页截图；合成 2 条真实结果、切页文本保留、刷新保持页面、参数自动落盘、自检 8 项通过。
- 文档：`docs/WEBUI_REBUILD.md`（设计/迁移）、`docs/WEBUI_GUIDE.md`（新界面说明）。
- 合并方式：仅需把 worktree 的 `frontend/`、`docs/WEBUI_REBUILD.md`、`docs/WEBUI_GUIDE.md` 带回主工作区；不要整分支合并（会带回旧后端）。
- 未提交、未推送；预览服务与 7860 后端当时仍在运行，结束前需按用户要求处理（2026-10-24 核验：当前已无相关进程在监听）。

---

## 11. 2026-10-24/25 WebUI 第三轮重做（工作台 IA，已并入主工作区）

用户对旧版与 worktree 重建版均不满意（"AI 味 + 又走老路"），经两轮设计稿迭代后定稿 C2：

- **设计依据**：联网研究了 ElevenLabs Studio / SaaSUI 截图库 / ElevenLabs UI（shadcn 基）与 shadcn.io、Creative Tim 的波形块。结论：真实产品的「产品感」来自近单色 + 细边框 + 黑主按钮 + 波形母题 + 克制徽章；IA 是任务为中心（台词画布居中、声音检查器在侧、建声音走向导），不是功能平级分页。
- **设计稿**：`design/A-bright.html`、`design/B-dark.html`（第一轮，被否）、`design/C-studio.html`（C2 定稿：工作台/声音库/记录 三目的地 + 新建声音向导 + 声音工作坊 + 记录表格 + ⌘K + 亮暗主题）。
- **新 IA**：导航只剩 工作台 / 声音库 / 记录；设置降级为弹窗；「采集 + 训练」合并为声音库下的「声音工作坊」五步引导流（上传素材→选说话人→校对→训练→完成）；合成结果直接长在每行台词下面（含 生成中/排队/失败/时长偏短/已完成 五态）；全局任务中心轮询 `GET /api/jobs`（训练可取消）。
- **实现**：主工作区 `frontend/src` 全量替换。复用 worktree 工程底座（lib/api.ts、synthesisStream、useConfig/useSynthesis/useReferenceAudio/useTraining/timbre/drafts/useToast、AudioPlayer、LanguageMarkerInput、ReferenceAudioReport）+ 新增 lib/waveshape.ts（确定性波形）、usePage（新页面表）、useVoices（GET /voice/options + 本地命名档案）、useJobs、useTheme、组件 WaveForm/TaskCenter/CommandPalette/SettingsModal；删除 three/AudioOrb/IconRail/TrainingOrbit 等全部旧物。视觉系统 `styles/tokens.css + base.css` 与 C2 同源（旧组件经 token 别名桥接复用）。
- **语言标记输入**：行块编辑器为逐行 textarea（Enter 分行 / Backspace 合并），语言标签自动补全暂未移植（旧 LanguageMarkerInput 是整篇文本组件），手打 `<en>` 仍有效。
- **命名建档缺口已关闭（2026-10-25 追记）**：发现后端本就有版本化声线卡接口（`GET/POST /voice-cards`、`/activate`、`/versions`，服务层 `voice_assets.VoiceCardStore`），前端已直接接入：建档向导完成后 `POST /voice-cards` 快照当前配置成卡，声音库展示卡片（版本号/状态/参考音可用性），「用于合成」走 `/activate` 写回配置。曾短暂实现的 POST /voice/profiles 方案已撤销（与声线卡重复）。工作坊校对仍用训练工作区转写（/api/training/transcript）。另：向导质检未达标改为「警告不拦截」（外部并行改动，保留）；7860 预览由用户自起的 python 进程（PID 1508）服务。
- **验证**：前端 49 项测试全过、`npm run build` 通过（170 kB JS / 32 kB CSS）；真实后端 7860 逐页截图核对（1440/1024/390，390 经 DOM 实测无横向溢出）；声音库/记录/工作台均吃到真实数据。
- 未提交、未推送；设计稿三份在 `design/`，供回顾。后端 7860 预览进程由会话启动，结束时应按用户要求处理。

---

## 12. 2026-11-01 端到端验收（胡桃视频）与四处缺陷修复

**验收**：33.7s 视频 → 媒体入库（1 说话人 / 10 切片 / 25.6s，Whisper 全量转写）→ 9 句少样本微调（s1 15 + s2 12，4.5 分钟）→ 46 行《光阴之外》朗读（245.9s，读回 CER 中位 3.85%，SECS 0.9195）。现场记录与产物在 `TEMP/acceptance_hutao/`（报告 `ACCEPTANCE_REPORT.md`、`full_novel.wav`、`novel.srt`、`report.json`、`dataset_snapshot/`）。

**当轮不改代码**，因此暴露了四处真实缺陷，随后单独一轮修复：

1. `tools/train.py: cmd_feat` 把 `GPT_SoVITS/prepare_datasets/`（目录）当脚本传给 Python → `/training/features` 必然 `can't find '__main__' module`。改为按脚本拼路径（`{prepare_dir}/{script}`）。
2. `cmd_train` 返回 `_ensure_s2_logs` 本体（需要 `root`），而 `start_job` 以 `pre()` 调用 → `/training/run` 立即 `[准备失败]`。改为 `partial(_ensure_s2_logs, root)`。
3. **API 路径缺 Gradio shim**：修好 (1) 后子进程立刻死在 `tools/my_utils.py → import gradio`。新增 `tools/train.py:_child_env()`（复用 `scripts/run_dataset_prep.ensure_shim_boot()` 生成的 sitecustomize + PYTHONPATH），特征提取与 s1/s2 训练都走它；`run_training_chain.ensure_boot_module()` 改为委托同一函数，CLI 与 API 不再漂移。
4. **参考音准备挂死（根因已确认）**：`media_ingest` 与 `voice_ingest` 各有一个 `lru_cache` 的 `_load_whisper`，媒体分析先加载 large-v3 后，`/reference-audio/prepare` 又在进程内加载**第二份**，8GB 卡被推满（实测 7.9GB / 98% 利用率）→ Windows 共享内存抖动，6.8s 音频「语音转写」卡 9 分钟。新增 `backend/services/whisper_cache.py`（进程级单实例 + `TRANSCRIBE_LOCK` 串行解码），两个模块的 `_load_whisper` 变成薄委托保留测试 seam。修复后同一序列：分析 15s、准备 **4.2s**，峰值显存 6.8GB（只多 96MB）。
5. **中文上传文件名乱码**：Starlette 用 latin-1 解 multipart filename，`胡桃.mp4` 变 `ºúÌÒ.mp4`，会话名/落盘名都受影响。新增 `backend/app._upload_filename()`（latin-1→utf-8 回转），用于参考音、训练切片、媒体入库、音色源、发音补丁五处。修后媒体会话的 `source/` 已落正确中文名。
6. **单字行读回误伤**：`噗！`（0.36s）被 SenseVoice 读回判 CER 200%，三次 take 全坏、还被标 `suspiciously_short`。`core.take_eligible` 增加「可读字数 ≥2」门槛，`_short_output_floor(text)` 对单字行只保留塌缩下限（0.15s）。

**验证**：`backend/tests/test_acceptance_fixes.py`（12 项）覆盖上述命令拼装、pre 零参、文件名回转、单字行判定；真实 API 复跑 特征提取（四步全 0 退出，dataset 落盘）与 `/training/run`（配置生成 → s1 → s2 `training done`，epochs=1 时按 `save_every=2` 不落盘属预期）。全量后端套件见本轮结果。

**注意**：`_guangpu_webui` 配置与 `custom_data` 在验收后已还原；胡桃素材为未授权商业游戏语音，声线卡 `vc_20261101_150126_484db8` 仅本地诊断留档。

## 0m. 2026-11-16 前端第三次推倒重建：设计稿 D · 单屏极简 · 浅色纸面

**过程**：用户要求「推倒之前的前端 UI 布局设计、只保留接口」。先并行产出三份静态设计稿供选（`design/D-focus.html` 单屏极简·浅色纸面 / `E-pro-three.html` 三栏后台·中性 / `F-flow.html` 流水线向导·克制深色，均经无头 Edge 截图核验），用户选定 **D**。

**实现（保留 lib/api、composables、synthesisStream/waveshape、AudioPlayer/WaveForm/ToastStack，重写视图层）**：
- `styles/tokens.css`：**变量名零改动**，浅色值换纸面色板（#FAF9F6 底/#191817 墨/2px 直角/轻投影），暗色主题块与旧组件别名桥接逐字未动（useTheme 不受影响）；`base.css` 删侧栏骨架改 D 顶部导航（.page 1240px/.nav 文字目的地/.lbl 等宽小标签），≤1120/≤700 两档媒体查询。
- `App.vue`：左侧栏改顶部导航（brand + 三目的地文字链 + autosave + 引擎状态点 + ⌘K 搜索 + TaskCenter + 设置齿轮 + 主题切换），全部接线保留（hash 路由、KeepAlive include="WorkbenchView"、五视图条件渲染、弹窗、初始化）。
- `WorkbenchView.vue` + `workbench/InspectorPanel.vue`：D 屏 1——voice-row/台词行（行号+15px 正文+line-result 五态：已完成/生成中进度条/排队/失败原因+重试/时长偏短警告）/gen-bar 大黑生成按钮；检查器 296px 五段（声音卡/语言 seg 自动+五语种/广普四档/参数预设 radio/高级参数 mono 数值输入仍走 650ms 防抖自动保存）；≤900px 检查器收「参数」抽屉（fixed 滑入+遮罩+ESC）；导出整集迁台词区头部 alink；InspectorPanel 不再有 generate emit（gen-bar 直调）。
- `VoicesView.vue` / `HistoryView.vue`：D 屏 2/3——vc-grid 声线卡（当前墨边/停用弱化/ghost 虚线）、三来源、工作坊五步条、kNN-VC 全保留；记录 table.rec 日期分组+新增搜索；两子流程页（Wizard/Workshop）仅换肤零行为改动。
- **0e 遗留闭合**：`lib/api/voiceCards.ts` 补 `patchVoiceCard`（后端契约 `PATCH /api/voice-cards/{id}` 请求体全可选经 voices.py:166 核实），types.ts/client.ts（补 api.patch 动词）同步；前端声线卡可停用/恢复。
- 壳层组件纸面化：TaskCenter（文字优先紧凑行）/SettingsModal（.lbl 四分区）/CommandPalette（mono 类型标签）/ToastStack（状态竖线）纯换肤零行为改动。
- **本轮修的两个真实 bug**：① `WorkbenchView` autoResize 只挂 @input，首屏长台词保持 1 行高被 overflow:hidden 竖裁——新增 `resizeAllLines`（挂载/lines 深度 watch/resize/onActivated 四时机）；② ≤700px 导航行超宽挤压换行——TaskCenter「任务」包 span.t 窄屏隐藏文字留图标+徽标，nav 间距收紧。
- 验证方式：旧版无头 Edge 直开窗口的视口语义不可靠，**以同源 iframe + `--virtual-time-budget` DOM 实测为准**：三页 390px 均 scrollWidth=视口宽、offenders 仅隐藏抽屉（诊断页已删）。
- **既有缺口（未修，如实记录）**：`useSynthesis.normalizeSnapshot` 未透传 duration/warning 字段，「时长偏短」只有兜底文案、结果不显示真实时长（重做前即如此）；后端 history 只记成功条目，记录页时长列显示「—」、warn/err 徽标样式已备不出现。

**验证**：`npx vue-tsc --noEmit` 0 错误；`npx vitest run` **58/58 全绿**（基线持平）；`vite build` 通过（JS 182.9KB / CSS 43.6KB）；三页 1440/1024/390 截图核验无横向溢出。未提交、未推送。`docs/WEBUI_GUIDE.md` 已同步新界面。

## 0n. 2026-11-17~21 环境抢救、kNN-VC 恢复、工作坊训练链路修复（用户首次 UI 全程训练成功）

**环境抢救**：
- **.venv 复活**：uv trampoline 指向的旧账户基础解释器（C:\Users\MouYu，profile 已消失）不可达——把本机已有的同版本 CPython 3.10.20（uv 管理）的原生 `python.exe + dll` 复制进 `.venv\Scripts`（旧 trampoline 备份 `python.exe.uv-trampoline.bak`），原生 exe 读旁边 pyvenv.cfg；site-packages 240 包零重装，torch 2.6.0+cu126 CUDA 可用。教训：uv venv 的 trampoline 内嵌创建时路径，改 pyvenv.cfg 无效。
- **vite 代理端口错位**：`vite.config.ts` 默认 `:8000`，文档写 7860——此前一切「HTTP 500」皆代理打空。默认改 7860（保留 VITE_API_TARGET）。

**kNN-VC 链路恢复**（曾因 third_party 缺失在 UI 裸报 FileNotFoundError）：
- ghfast.top 镜像克隆 `third_party/knn-vc` + 断点续传下载 WavLM-Large.pt(1.26GB，GitHub 直连会被重置)/prematch_g_02500000.pt；
- **深层冲突**：GPT-SoVITS `AR/modules/activation.py` 全局替换 `F.multi_head_attention_forward`，其补丁文件只显式导入 `_in_projection_packed` 漏了 `_in_projection`（torch 2.6 另一分支需要）→ 引擎加载后 wavlm 必炸 NameError。已在 `patched_mha_with_cache.py`/`_onnx.py` 补齐导入（onnx 变体连 `_mha_shape_check/_none_or_dtype/_in_projection_packed` 一起补）；
- 服务端到端验证：/api/timbre/convert-file 真实转换成功。

**工作坊训练链路修复（用户 11-17 首训失败 + 11-21 全程跑通）**：
- 用户首训失败根因不是步骤错：单句素材切片仅 1 段，GPT-SoVITS s1 dataloader 对迷你数据集 `ZeroDivisionError`（dataset.py:186）——解法是合并素材（12 段胡桃音频 ffmpeg 拼接为 1 分 42 秒单文件，工作坊一次上传自动切 15 段）；
- **转写 PUT/POST 错配**：后端 `/training/transcript` 是 PUT，前端 runAction 发 POST → 405，「保存并继续」从未成功过；
- **空清单覆盖事故**：页面在转写加载前点保存，把 corrected 清单覆盖成 0 行 → 特征提取「成功」处理零行 → 训练必崩。修复三层：后端守卫（whisper 清单有内容时拒存空清单，test_transcript_guard.py 3 项）+ 工作坊 activate 后自动加载转写 + 刷新自动恢复 + 校对编辑 800ms 防抖自动保存（静默 PUT）；
- **prepare 脚本「产物已存在就整步跳过」**：换数据集后旧 `2-name2text/6-name2semantic` 残留让 text/semantic 对 15 段新素材整步跳过（还曾把旧 tsv 当新数据）。修复：`training.py:clean_stale_prepare_artifacts` 作为 features 任务 pre() 清 opt_dir（2-name2text*/6-name2semantic* + 3-bert/4-cnhubert/5-wav32k/7-sv_cn 内容），tools/train.py shim 转发；
- 特征提取子进程 traceback 被 try/except 打印后**正常退出 0**（prepare 脚本逐行 try 吞错），rc 无法反映数据问题——数据正确性靠上面的守卫与清理保证；
- **选窗契约对齐**：voice_ingest 选窗下限 1.8s vs 引擎要求参考音 ≥3s——2.6 秒素材建档后合成必败。MIN_SELECTED_SECONDS=3.0，兜底候选不足 3s 不再发出，错误文案可操作；
- **训练流程已在 UI 全程由用户跑通**：merged 文件 → 15 段 → 特征提取 → s1 15 + s2 12 → `Hutao-e14.ckpt/Hutao_e12_s288.pth` → ⑤ 扫描启用 → 端到端合成成功（对比 11-17 零样本版）。

**声线卡管理补全**：
- `DELETE /api/voice-cards/{id}`：VoiceCardStore.delete（rmtree 卡目录），路由守卫「正在用于合成的卡先切换再删」，前端红色「删除」两步确认（再点一次确认模式），test_voice_cards_api.py 补 3 项；
- 缺参考音的卡「用于合成」置灰（判定与「参考音缺失」徽标同源 timbre_available；曾误用文件名判定，路径字符串恒真）；
- 新建声音向导直跳第 4 步的 bug：步进判定 `if (config.ref_audio) return 4` 把配置里常驻参考音当成本次建档——改为只由本次向导上传/质检状态驱动。

**调试基建（2026-11-21 加入，稳定后可降噪）**：前端 `[xianghua]` 前缀日志——app boot、全部 API 调用（方法/路径/状态/耗时）、window error/unhandledrejection、捕获阶段全局点击探针（坐标+trusted+光标下真实元素）。排查案例：用户「点击没反应」最终定位为**浏览器缩放/坐标错位**（点击命中导航而非目标按钮，Ctrl+0 解决）；CDP 真浏览器驱动测试曾误删用户 MouYu 声线卡，已用会话内保存的完整卡片 JSON 原样重建（voice_cards/vc_20261025_125337_d8c8d6/card.json 手工恢复，无损）。教训：对用户数据做删除类验证必须用一次性冒烟卡，不能点真实列表首位。

**既有缺口（未修，如实记录）**：`useSynthesis.normalizeSnapshot` 未透传 duration/warning（「时长偏短」只有兜底文案）；后端 history 只记成功条目（记录页时长列「—」）；`/api/jobs/{id}` 对特征提取类任务返回 404（feat 任务只进 trainstate 汇总，前端轮询需走 /training/status）；prepare 子进程吞错使 rc 不可信（数据正确性由守卫保证，未加子进程级校验）。

**验证**：前端 tsc 0 错误 + 58/58；后端本轮相关 23+14+8 项测试全过、ruff 全绿；训练产物与端到端合成均已核验。未提交、未推送。

