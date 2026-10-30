# 项目持久记忆

更新时间：2026-10-25
状态：清晰优先基线已用本人真实声音验证；VoiceCard/StylePack 资产契约已落地

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
- `third_party/knn-vc/`：当前音色转换链路仍在用；
- `.worktrees/`：两个旧 worktree 仍有未提交改动，未删除，需用户确认后再处理。

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
- kNN-VC 语音音色转换链路保留：`scripts/knnvc_runtime.py`、`backend/services/timbre.py`、`third_party/knn-vc/`。

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
