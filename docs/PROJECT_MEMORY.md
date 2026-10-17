# 项目持久记忆

更新时间：2026-10-17
状态：清场后的新起点

这份文档是后续会话的项目级记忆入口。开始任务前先读 `AGENTS.md` 和本文件；如果知识图谱已被重建，再检查 `.code-review-graph/graph.db`。当前图谱已在清场时删除，后续按需重建。

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

---

## 3. 当前代码事实

- 后端：FastAPI，入口 `backend/app.py`；当前是本地单用户 API，不是多租户 B2B API。
- 前端：Vue 3 + Vite，入口 `frontend/src/App.vue`；页面为合成、训练、历史、设置、音色。
- 引擎：GPT-SoVITS v2Pro；当前默认配置已切到“清晰优先”：
  - `gpt_model = GPT_SoVITS/pretrained_models/s1v3.ckpt`
  - `sovits_model = GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth`
  - `ref_audio = custom_data/sliced/训练.wav_0000018880_0000143680.wav`
  - `ref_text = 今天天气真不错，我们出去走走吧。`
  - `auto_lightmid = false`
- 训练管线仍在：`scripts/run_dataset_prep.py`、`scripts/run_training_chain.py`、`scripts/verify_training_loop.py`、`tools/train.py`。
- 声音档案/口音包抽象仍在：`backend/services/voice_selection.py`、`assets/accent_packs.json`；口音包资产尚未补齐。
- kNN-VC 语音音色转换链路保留：`scripts/knnvc_runtime.py`、`backend/services/timbre.py`、`third_party/knn-vc/`。

---

## 4. 验证状态（2026-10-17 清场后）

- 后端 unittest：127 tests，0 failures，2 errors（均为未安装 pytest 的既有导入错误）。
- 前端：121 tests 全过；`npm run build` 成功。
- `create_app()` 可导入，注册 45 条路由。

---

## 5. 已确认的问题

1. `tools/rules.py` 的“轻中浓度”是全局替换：所有“的/是”都会被替换，技术句会被改成不自然的广普写法，显著降低可懂度。
2. G2P 补丁仍在 `GPT_SoVITS/text/chinese2.py` 和训练特征里；改字典需要重建特征/重训，尚未外置成 `PronunciationPlan`。
3. 旧训练集 64 条、281.7 秒，其中 52 条/226.8 秒是浓广普，清晰普通话只有 12 条/54.9 秒；模型会把浓口音学进音色。
4. 旧 WebUI 配置曾出现参考音频与参考文本不配对；当前配置已修正，但 UI 仍缺自动配对校验。
5. B2B API 尚缺：API Key、租户、配额、持久化队列、项目/角色、voice version、license/consent、Webhook、水印、公开 SDK。
6. 旧工作区有 8 个未推送 commit 和一批未提交改动；清场后需重新审查，不要直接覆盖。

---

## 6. 下一步优先级

1. 建 20 句固定回归集：清晰普通话、技术句、轻/中/浓广普；固定 seed、参考音、模型版本。
2. 把 `tools/rules.py` 改成 `off / light / medium / heavy` 档位，保护固定搭配，先写测试。
3. 给参考音/文本增加配对校验，WebUI 选择切片时自动带出对应文本。
4. 用“清晰优先”预设跑基线，确认 CER/SECS 后再动训练。
5. 录 5–10 分钟清晰普通话（80% 清晰 + 20% 轻中口音），训练 clear-dominant 模型。
6. 设计并实现 `VoiceCard`、`PronunciationPlan`、`AccentPack`、`StylePack` 四个数据契约。
7. 再做 Managed Training pilot、B2B API 骨架和 license ledger。

---

## 7. 工作边界

- **不要替用户 commit / push**；改动保留在工作区。
- 不要执行 `git reset`、`git checkout --` 等破坏性操作。
- 不要删除 `custom_data/raw/`、`sliced/`、`asr/` 和最终权重；它们是用户资产。
- 不做无授权声线爬取；只接受本人、签约配音演员、版权方授权或明确许可的素材。
- 不先加功能；TTS 可懂度不过线前，不做 API 平台化。
- 需要提交时，只在最终回复列出待提交文件清单和中文 commit 摘要。
