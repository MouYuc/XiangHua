# 无主观盲听的客观评测方案：方言 TTS / 多语种配音 / 歌声音色转换

> 调研日期：2026-09-29 ｜ 面向对象：DiaMoE-TTS 方言合成、GPT-SoVITS 五语种配音、kNN-VC 歌声转换
> 硬件约束：单卡 RTX 3070 Ti（8 GB VRAM）、Python 3.10、torch 2.6.0+cu126

## 0. 调研方法与可靠性声明（务必先读）

- 本次会话的 `web_search` 工具因服务端计费问题**不可用**，全部事实来自 `web_fetch` 直抓原始页面：arXiv API（`export.arxiv.org/api/query`）、arXiv HTML 全文（`arxiv.org/html/...`）、GitHub raw / GitHub API、OpenReview/OpenDataLab。HuggingFace 直连在本环境被网络阻断（`fetch failed`），凡仅依赖 HF 页面的结论均已标注。
- **arXiv ID 一律从检索结果取得，绝不凭记忆填写。** 调研中确实发生过"凭印象猜 ID 而猜错"的情况（例如 2105.01441 并非 NISQA），已全部用 API 检索结果替换。
- 标记约定：
  - 「**未找到可靠出处**」= 我尝试检索但无法核实；
  - 「**未取到原文定义句**」= 论文存在且链接可打开，但全文抓取在目标段落前被截断，我没有读到逐字定义；
  - 「**本项目估算**」= 由已核实数据推算，非文献结论。

---

## 1. 结论速览

| 产物 | 内容正确性 | 方言/音色身份 | 自然度 | 必须自训的部分 |
|---|---|---|---|---|
| ① 方言 TTS | 方言 ASR 的 CER（**不能用普通话 ASR**） | **DID 分类准确率 + 方言嵌入余弦相似度** | UTMOS + DNSMOS | **中文方言 DID 分类器** |
| ② 五语种配音 | 分语种 ASR 的 CER/WER | SECS（WavLM-SV） | UTMOS + DNSMOS | 无（可用现成模型） |
| ③ 歌声音色转换 | ASR CER（唱词）+ F0 保真 | SECS（目标音色↑、源音色↓） | 歌唱 MOS 预测器 | **歌唱 MOS 预测器**（SingMOS 微调） |

三条最关键、且**有文献正面支持**的判断：

1. **"CER 方向反了"不是你的实现 bug，是被权威文献明确记录的现象。** Seed-TTS 原文："a lower WER does not necessarily lead to an improved subjective score on speaker similarity … a lower WER typically indicates that the model produces more 'standardized' speech … in cases where the prompt speech contains a strong accent …, obtaining a lower WER from generated speech usually indicates less accented speech."（[arXiv:2406.02430](https://arxiv.org/abs/2406.02430)，§3.1）→ 结论：方言任务里 **CER 只能当"可懂度护栏"，绝不能当方言保真度指标**。
2. **"用 DID 分类器 + 方言嵌入余弦相似度做方言真实性分数"有直接先例**：TMD-TTS（藏语三方言）用 **DCA（方言分类准确率）** 与 **DECS（方言嵌入余弦相似度）** 作为核心客观指标，并在数据流水线里用 **DECS > 0.8** 作为筛选阈值（[arXiv:2509.18060](https://arxiv.org/abs/2509.18060)）。
3. **不存在可直接零训练使用的中文方言级 DID 模型**（详见 A3）。现成的都是"语种级 LID"，最细只到"普通话 vs 粤语"。这一项**必须自训**，但成本远低于想象。

---

## 2. A. 方言真实性（dialect authenticity）如何客观度量

### A1. 中文（子）方言辨识 DID 的 SOTA 做法与可复现实现

| 方案 | 论文 | 骨干/池化 | 报告准确率 | 代码 |
|---|---|---|---|---|
| KeSpeech 官方基线 | [KeSpeech, Tang et al., NeurIPS 2021 D&B](https://openreview.net/forum?id=b3Zoeq2sCLq) | Kaldi-xvector / ResNet-34 / ECAPA-TDNN | **56.34 / 61.13 / 60.77 %**（6 个官话次方言） | [github.com/KeSpeech/KeSpeech](https://github.com/KeSpeech/KeSpeech)（仅数据与说明） |
| Whisper-small + 分类头（MEFT） | [Lin et al., APSIPA ASC 2025, arXiv:2512.02074](https://arxiv.org/abs/2512.02074) | Whisper-small encoder（88M 含头）+ 256 维投影 + 均值池化 + FC | Vanilla FT **78.67 %**；LoRA r=256 **78.77 %**；LST RF=8 **78.00 %**；仅训头 **59.33 %** | [github.com/linzr25/Whisper-DI-MEFT](https://github.com/linzr25/Whisper-DI-MEFT) |
| MMGER（ASR+口音联合，生成式纠错） | [Mu et al., arXiv:2405.03152](https://arxiv.org/abs/2405.03152) | 多模态多粒度 GER + LLM | KeSpeech 口音识别准确率相对提升 **26.72 %**，ASR CER 相对降 27.55 % | 未核验 |

**可直接照抄的工程结论**：在 8 类/6 类方言上，**Whisper-small 冻结 encoder + 线性分类头可达 ~59 %，LoRA 微调可达 ~78 %**。也就是说"7 类方言、每类几千条"的自训 DID 是**成熟配方**，而不是研究难题。

### A2. KeSpeech 事实核查（规模、类目、许可、下载、磁盘）

已核实（[arXiv:2512.02074](https://arxiv.org/abs/2512.02074) §4.2 原文）：**1,542 小时音频、27,237 位说话人、覆盖中国 34 个城市，含标准普通话与 8 个次方言**，且带转写、说话人身份、次方言三重标注，并含"普通话与当地方言平行录音"（这一点对 TTS 极有价值：可直接做方言风格迁移的平行评测）。

8 个次方言（[arXiv:2505.21138](https://arxiv.org/abs/2505.21138) §3.1 原文列举）：**北京、江淮（Jiang-Huai）、胶辽（Jiao-Liao）、冀鲁（Ji-Lu）、兰银（Lan-Yin）、东北（Northeastern）、西南（Southwestern）、中原（Zhongyuan）**。

**⚠️ 对你 8 个目标城市的覆盖率（我按方言分区推断，需你自己再核）**

| 你的城市 | 所属官话/方言区 | KeSpeech 是否覆盖 |
|---|---|---|
| 成都 | 西南官话 | ✅ Southwestern |
| 石家庄 | 冀鲁官话 | ✅ Ji-Lu |
| 郑州 | 中原官话 | ✅ Zhongyuan |
| 西安 | 中原官话（关中片） | ✅ Zhongyuan |
| 武汉 | 西南官话（武天片） | ✅ Southwestern |
| 南京 | 江淮官话 | ✅ Jiang-Huai |
| 青岛 | 胶辽官话 | ✅ Jiao-Liao |
| **上海** | **吴语（太湖片）** | ❌ **不在 KeSpeech 的 8 类中** |

→ 上海（吴语）与官话次方言不同层级，**KeSpeech 无法为它提供训练/评测数据**；DiaMoE-TTS 自己的表 2 里 SH（上海）WER 76.53、MOS 1.80，也是全表最差之一，与"上海话最难"一致。

**许可（已逐字核验）**：`dataset_license.md` 明确三条硬约束——**① 非商业性使用**（不得用于任何商业目的，含商业优势、金钱偿付或数据交换）；**② 允许技术性修改，禁止演绎（No Adaptations）**；**③ 禁止向第三方分发**。→ 若你的项目有任何商业化前景，**不要基于 KeSpeech 训练并分发模型权重**（是否构成 Adaptations 需法务判断，风险点明确存在）。

**下载方式（已核验）**：官方仓库 README 仅提供**百度网盘链接**（`pan.baidu.com/s/1VaHniaQDVht30TZg3RAwCA`，提取码 `b6fy`）+ 许可协议；OpenDataLab 页面为纯 JS，正文未取到。

**磁盘估算（⚠️ 本项目估算，非文献数据）**：
- 官方**未公布**压缩包/分片大小 → 分片大小这一点「**未找到可靠出处**」。
- 按 1,542 h、16 kHz、单声道、16-bit PCM 估算：1542×3600×16000×2 B ≈ **178 GB**（十进制）/166 GiB。若官方是压缩包，还需额外解压空间。
- **判断：215 GB 属于"擦边甚至不够"**（下载压缩包 + 解压峰值可能 > 215 GB）。建议：**不要全量下载**，按需只取你要的 6 个官话类别的子集；先下 1~2 个分片确认体积再决策。

### A3. 能否用现成公开模型零训练得到方言置信度？

| 模型 | 标识 | 标签空间 | 能否区分中文方言 |
|---|---|---|---|
| SpeechBrain VoxLingua107-ECAPA | 论文 [Valk & Alumäe, SLT 2021, arXiv:2011.12998](https://arxiv.org/abs/2011.12998)（107 语种，6628 h） | 107 种**语言** | ❌ 中文是**一个类** |
| SpeechBrain CommonLanguage-ECAPA | HF `speechbrain/lang-id-commonlanguage_ecapa` | 45 种语言 | ❌（HF 页面本环境被阻断，**未核验**其标签是否含 `zh-yue`） |
| FunASR SenseVoiceSmall | [arXiv:2407.04051](https://arxiv.org/abs/2407.04051)，HF/ModelScope `FunAudioLLM/SenseVoiceSmall`、`iic/SenseVoiceSmall` | **zh / en / ja / ko / yue**，234M | ⚠️ 只能分出"粤语 vs 普通话"，**分不出官话次方言** |
| FunASR Fun-ASR-Nano | `FunAudioLLM/Fun-ASR-Nano-2512`，800M | zh / en / ja + **"Chinese dialects and accents"** | ⚠️ README 只说覆盖方言与口音，**未说明是否输出方言标签** → 「**未找到可靠出处**」证明它有方言分类头 |
| 3D-Speaker / WeSpeaker 的 LID 配方 | [3D-Speaker](https://github.com/modelscope/3D-Speaker)、[WeSpeaker](https://github.com/wenet-e2e/wespeaker) | 3D-Speaker LID 配方为 **Mandarin / English** | ❌ |
| CAM++ / ERes2NetV2 / ECAPA-TDNN | `iic/speech_campplus_sv_zh-cn_16k-common` 等 | 说话人身份（非方言） | ❌（但可作方言嵌入的骨干，见 A4） |

**结论：零训练拿不到 8 类中文方言的置信度。** 唯一"零训练可用"的近似是 Fun-ASR-Nano（方言/口音 ASR），但它给出的是**转写质量**而非方言身份。

### A4. 关键：如何把 DID 用成"方言语义正确性"分数（文献先例）

**这是本次调研最重要的发现，你的诉求在文献里已有成熟范式：**

| 先例 | 做法 | 出处 |
|---|---|---|
| **TMD-TTS（藏语三方言）** | 客观指标 = **STOI / PESQ / SI-SDR / DNSMOS / DCA / DECS / RTF**；主观 = nMOS + dMOC（20 位母语者）。其中 **DCA = 预训练方言分类器给出的方言分类准确率**；**DECS = 方言嵌入余弦相似度**（度量方言一致性）；数据流水线用 **DECS > 0.8** 保留样本，PESQ<3 或 DNSMOS<2.7 用 MetricGAN+ 增强 | [arXiv:2509.18060](https://arxiv.org/abs/2509.18060) §2.2/§3.1 |
| TMD-TTS 的消融 | DCA 从 80.25% 掉到 33.42% 能线性反映"方言信息被移除" | 同上 Table 2 |
| 阿拉伯语多方言 TTS（NADI 2026 shared task） | 同一 shared task 内同时设 **ASR / Spoken Dialect Identification (SDID) / TTS** 子任务 | [arXiv:2609.27086](https://arxiv.org/abs/2609.27086) |
| 阿拉伯语零样本方言 TTS | 显式**使用一组阿拉伯语方言辨识模型**来研究方言标签对 ZS-TTS 的影响 | [arXiv:2406.16751](https://arxiv.org/abs/2406.16751) |
| FMSD-TTS | 公开"方言一致性/说话人相似度/音频质量"标准化评测工具包 | [arXiv:2505.14351](https://arxiv.org/abs/2505.14351)（该文已撤稿，改投 2509.18060） |

**因此，"DID 置信度 / 方言分类准确率"作为方言 TTS 的客观验收指标，是有文献支撑的主流做法，不是自创。** 相比之下，**"用方言 ASR 的 CER 评测方言 TTS"我没有找到把 CER 作为方言保真度指标的专门先例**——DiaMoE-TTS 自己用的是 WER（当作可懂度），Habibi 用"自动指标 + 主观"（[arXiv:2601.13802](https://arxiv.org/abs/2601.13802)）。→ 建议把**方言 ASR CER 定位为"内容正确性护栏"**，把**DID 定位为"方言身份"**，各司其职。

### A5. 可获取的方言 ASR 模型与 CER 量级

| 模型 | 规模 | 许可/来源 | 方言场景实测 CER（原文数字） |
|---|---|---|---|
| **Fun-ASR-Nano** `FunAudioLLM/Fun-ASR-Nano-2512` | 800M | FunASR Model Zoo（[README](https://raw.githubusercontent.com/modelscope/FunASR/main/README.md)） | — |
| **SenseVoiceSmall** | 234M | zh/en/ja/ko/**yue** | — |
| paraformer-zh | 220M | 普通话 | — |
| Whisper-large-v3 | 1550M | — | KeSpeech **31.06**；河南 **87.78**；上海 **87.78**；粤语 **55.51**；湖南 **83.61** |
| Qwen2-Audio-Instruct | — | — | KeSpeech **10.88**；河南 45.06；上海 49.89；粤语 17.72；湖南 66.19 |
| MMGER | — | — | KeSpeech **7.52** |
| TeleSpeech（Data2vec2 301M + Qwen2-7B） | — | [arXiv:2505.21138](https://arxiv.org/abs/2505.21138) Table 3 | KeSpeech **6.48** |

**重要警告（有出处）**：同一篇论文明确指出 KeSpeech 的"方言味并不浓"——"the accents in this dataset are not particularly pronounced, likely because regional labels for speakers were directly assigned as accent labels, even when many speakers exhibit minimal accent features"（[arXiv:2505.21138](https://arxiv.org/abs/2505.21138) §3.1）。→ **用 KeSpeech 训练出来的 DID/ASR 去判"成都味足不足"会系统性偏保守**；验收时必须以"真人方言参考音"跑同一个模型取 baseline，用**相对值**而不是绝对值。

**四川话/上海话专用 ASR 模型**：我在可检索范围内**未找到可靠出处**（没有找到公开可下载、有明确模型卡与 CER 报告的四川话/上海话专用 ASR）。

### A6. 专门做"方言 TTS 评测"的论文

| 论文 | 评测指标 |
|---|---|
| **DiaMoE-TTS**（你自己的系统）[arXiv:2509.22727](https://arxiv.org/abs/2509.22727) | WER + MOS + **UTMOSv2**；含 YUE/SH/CD/XA/ZZ/TJ/NAN/SJZ/NJ/Jingbai/Yunbai 多方言表 |
| Bailing-TTS [arXiv:2408.00284](https://arxiv.org/abs/2408.00284) | 论文摘要未列具体客观指标 |
| TMD-TTS [arXiv:2509.18060](https://arxiv.org/abs/2509.18060) | STOI/PESQ/SI-SDR/DNSMOS/**DCA**/**DECS**/RTF + nMOS/dMOC |
| Cross-Dialect TTS (Yamauchi et al., SLT 2024) [arXiv:2409.07265](https://arxiv.org/abs/2409.07265) | 方言自然度（对比基线） |
| Habibi（阿拉伯语多方言 TTS 基准）[arXiv:2601.13802](https://arxiv.org/abs/2601.13802) | 首个标准化多方言 TTS benchmark，>11,000 条、7 个方言子集 |
| NTU-AISG, Blizzard Challenge 2020（**上海话** TTS track）[arXiv:2010.11489](https://arxiv.org/abs/2010.11489) | 主观评测：**合成语音自然度与原说话人相似度尚可，但可懂度被严重削弱** |

> 说明：UTMOSv2 被 DiaMoE-TTS 论文使用，但我**未找到**名为 "UTMOSv2" 的 arXiv 论文或官方仓库 → 「**未找到可靠出处**」（可能指 UTMOS strong learner 的更新版，需你按自身需求另行确认）。

---

## 3. B. 音色相似度怎么客观度量

### B1. SECS 的标准做法与数值区间

**标准做法（已核验）**：取**在说话人验证任务上微调过的 WavLM-Large** 提取说话人嵌入，对"合成音频 vs 参考音频"求**余弦相似度**。

- 权威参考实现：[seed-tts-eval](https://github.com/BytedanceSpeech/seed-tts-eval)（Seed-TTS 官方评测集+脚本）。原文："For SIM, we use **WavLM-large fine-tuned on the speaker verification task** to obtain speaker embeddings used to calculate the cosine similarity of speech samples of each test utterance against reference clips."（[arXiv:2406.02430](https://arxiv.org/abs/2406.02430) §3.1）
- 脚本：`bash cal_sim.sh {meta} {synth_dir} {wavlm_large_finetune.pth}`；checkpoint 见该仓库 README（Google Drive 链接）。ASR 侧：英文 Whisper-large-v3，中文 paraformer-zh。

**文献中报告的正常区间（逐字取自原文表格）**

| 论文 | 条件 | SIM |
|---|---|---|
| Seed-TTS | 中文 Seed-TTS 零样本 ICL | **0.796** |
| Seed-TTS | 中文真人（同说话人不同句，作为上界参照） | **0.750** |
| Seed-TTS | 中文 codec 重建语音 | 0.733 |
| Seed-TTS | 英文 Seed-TTS / 真人 / 重建 | 0.762 / 0.730 / 0.702 |
| Seed-TTS | 说话人微调（SFT） | 0.789 |
| Seed-TTS | 零样本 VC：本文自蒸馏前 / 后 | 0.491 / **0.753** |
| Seed-TTS | VC 对照：DiffVC / HierSpeech++ | 0.311 / 0.387 |

**可操作的判读基准（本项目建议，依据上表）**：
- 用**同一嵌入模型**先测**真人同说话人不同句**的 SECS，作为"同一人上界"（该模型下约 **0.73–0.75**）；
- 验收门槛建议设为**"上界的 90% 以上"**（即约 **0.66–0.70**），而不是死记 0.7/0.8；
- 高质量克隆目标：**≥0.75**。
- ⚠️ **SECS 绝对值强烈依赖嵌入模型**（CAM++/ERes2Net/WavLM-SV 之间不可直接比较），**必须先在本项目数据上标定一次真人上界**。

**注意**：kNN-VC 原论文并未使用 SECS，而是用 **x-vector EER**（[arXiv:2305.18975](https://arxiv.org/abs/2305.18975) §4.3：follow [22]，用 x-vector 说话人验证系统 [23] 计 EER）。EER 在该文语境下 **越高越好（上限 50%）**，与常规"EER 越低越好"相反，读表时务必注意。

### B2. SIM-o / SIM-r 的区别

- **有可靠出处的部分**：seed-tts-eval 仓库为 VC 任务提供的是 `en/non_para_reconstruct_meta.lst` / `zh/non_para_reconstruct_meta.lst`（"reconstruct" 元文件），即评测时用作对照锚点的参考语音是**经 codec 重建后的参考语音**——这正是 SIM-r 协议所指（与"重建参考"比），而普通 TTS 任务用的是原始 prompt（SIM-o）。（[seed-tts-eval README](https://github.com/BytedanceSpeech/seed-tts-eval)）
- **未取到原文定义句**：业界普遍把 SIM-o / SIM-r 的首次区分归于 **NaturalSpeech 2（Shen et al., [arXiv:2304.09116](https://arxiv.org/abs/2304.09116)）§4.4**；我多次抓取其 HTML 均在 §3.2 处被工具截断，**没有读到逐字定义**，因此这里明确标注为「**未取到原文定义句**」，请你落地时以原文 PDF 复核。

### B3. 说话人嵌入模型清单与 8 GB 可行性

| 模型 | 参数量（**来源已核验**） | VoxCeleb1-O EER（来源已核验） | 获取 | 8 GB 推理 |
|---|---|---|---|---|
| **CAM++** | **7.2 M** | **0.65 %** | `iic/speech_campplus_sv_zh-cn_16k-common`、HF `funasr/campplus`、FunASR `spk_model="cam++"` | ✅ 极轻松 |
| **ERes2NetV2** | **17.8 M** | **0.61 %** | `iic/speech_eres2netv2_sv_zh-cn_16k-common`（20 万说话人中文训练）、`iic/speech_eres2netv2w24s4ep4_sv_zh-cn_16k-common`（你项目已用 w24s4ep4） | ✅ 轻松 |
| ERes2Net-large | 22.46 M | 0.52 % | 3D-Speaker | ✅ |
| ECAPA-TDNN | 20.8 M | 0.86 % | `iic/…`、WeSpeaker、SpeechBrain `spkrec-ecapa-voxceleb` | ✅ |
| ResNet34（WeSpeaker，LM+AS-Norm） | 6.34 M | 0.723 %（vox1-O-clean） | WeSpeaker `wespeaker.load_model('chinese')` | ✅ |
| deep r-vector 293 层 | — | 0.447 % | WeSpeaker | ✅ |
| WavLM-Large SV | — | — | seed-tts-eval 的 `wavlm_large_finetune.pth` | ⚠️ 参数量**未从可靠来源取到**；按 24 层×1024 hidden 估算约 3 亿参数级（fp16 权重约 0.6 GB，**本项目估算**），8 GB 推理可行但显存占用明显高于 CAM++ |

（3D-Speaker 参数量/EER 表：[github.com/modelscope/3D-Speaker](https://github.com/modelscope/3D-Speaker) README Benchmark 表；WeSpeaker EER：[wespeaker README](https://github.com/wenet-e2e/wespeaker)。）

**推荐**：中文场景**主力用 ERes2NetV2(w24s4ep4)（你项目里已有该权重）**，**副用 WavLM-Large-SV（对齐文献惯例、便于跨论文比较）**。两者都报告，不要混用其绝对值。

---

## 4. C. 音质/自然度的无参考客观指标

### C1. UTMOS

| 项 | 内容 |
|---|---|
| 论文 | **UTMOS: UTokyo-SaruLab System for VoiceMOS Challenge 2022**，一作 **Takaaki Saeki**，2022，Interspeech 2022，[arXiv:2204.02152](https://arxiv.org/abs/2204.02152) |
| 实现 | [github.com/sarulab-speech/UTMOS22](https://github.com/sarulab-speech/UTMOS22)；HF Space `sarulab-speech/UTMOS-demo`（克隆后 `python predict.py --mode predict_file --inp_path x.wav --out_path o.csv`） |
| 训练数据 | VoiceMOS Challenge 2022 **Main Track Dataset**（Blizzard + Voice Conversion Challenge 历史主观打分） |
| 输入/输出 | 单声道 wav 文件 → 预测 MOS，**1–5** 分（含系统级/语句级聚合） |
| 8 GB | ✅ 无压力（SSL 骨干 + 轻量回归头） |
| 原论文成绩 | VoiceMOS Challenge 2022 **16 项指标中 10 项第一** |

### C2. DNSMOS

| 项 | 内容 |
|---|---|
| 论文①| **DNSMOS: A Non-Intrusive Perceptual Objective Speech Quality Metric to Evaluate Noise Suppressors**，Reddy / Gopal / Cutler，ICASSP **2021**，[arXiv:2010.15258](https://arxiv.org/abs/2010.15258) |
| 论文②（推荐用这个） | **DNSMOS P.835**，Reddy / Gopal / Cutler，ICASSP **2022**，[arXiv:2110.01763](https://arxiv.org/abs/2110.01763) |
| 实现 | [microsoft/DNS-Challenge → DNSMOS/dnsmos_local.py](https://github.com/microsoft/DNS-Challenge)（ONNX 推理；`-p` 开关启用"个性化 MOS"，惩罚干扰说话人） |
| 输出 | P.835 三项 **SIG / BAK / OVRL**，1–5 |
| 原论文相关性 | 作者报告 PCC：**SIG 0.94、BAK 0.98、OVRL 0.98**（在其评测条件下） |
| 8 GB | ✅ 无压力 |

### C3. NISQA / SQUIM / SQUID 值不值得用？

| 指标 | 论文/实现 | 结论 |
|---|---|---|
| **NISQA** | Mittag et al., Interspeech 2021, [arXiv:2104.09494](https://arxiv.org/abs/2104.09494)；[github.com/gabrielmittag/NISQA](https://github.com/gabrielmittag/NISQA)（链接经 UTMOS 官方 README 核验）。输出总体质量 + Noisiness / Coloration / Discontinuity / Loudness 四维，训练/评测覆盖 81 个数据集 | **可选，作补充**。它针对**通信网络失真**设计（噪声、混响、丢包），对"合成语音的编解码/底噪瑕疵"有诊断力，但对**韵律自然度**没有覆盖。若你只想抓"电音/沙哑/底噪"，可用它做**归因诊断**，不做主指标 |
| **SQUIM** | Kumar et al., ICASSP 2023, [arXiv:2304.01448](https://arxiv.org/abs/2304.01448)；已进 **torchaudio** | **建议用作"参考型指标的无参考替身"**：无参考估计 PESQ / STOI / SI-SDR / MOS。装 torchaudio 即可，**零额外依赖、零训练**，非常适合放进 CI 当回归护栏 |
| **SQUID** | "Squid: Measuring speech naturalness in many languages"（在 SingMOS 参考文献中出现，见 [arXiv:2406.10911](https://arxiv.org/abs/2406.10911) 引文 [7]） | **潜在价值高（多语言自然度）但本次未逐条核验其 arXiv ID/API** → 「**未找到可靠出处**」，建议你自行确认后再用 |

### C4. 已知局限（有出处）

1. **域偏移（speech → singing）**：SingMOS 明确指出，语音 MOS 预测模型"mostly trained and tested exclusively on speech data"，而语音与歌唱"in terms of higher requirements for pitch accuracy and naturalness"存在域差，直接迁移困难（[arXiv:2406.10911](https://arxiv.org/abs/2406.10911) §1）。
2. **可被对抗攻击**：Wen-Chin Huang & Tomoki Toda 的 *Attacking UTMOS*（**SLT 2026**, [arXiv:2606.31105](https://arxiv.org/abs/2606.31105)）证明存在**保分降质攻击**（score-preserving attack 有效）与**降分保质**攻击（在 EnCodec 隐空间最易成功）→ **不要把 UTMOS 当唯一护栏，它可被"刷分"**。
3. **DNSMOS 的设计目标不是 TTS 自然度**：其训练标签来自 **ITU-T P.808 / P.835** 的主观打分，场景是**噪声抑制**；对干净合成语音容易**饱和**（TMD-TTS 表中 DNSMOS 普遍落在 2.77–3.55 的窄带内，区分度有限）。
4. **MCD 类频谱距离与感知弱相关**：SingMOS 引言原文："other subjective metrics like Mel-cepstral distance (MCD) show less correlation with perceptual audio quality …, especially with recent high-fidelity synthesis models" —— 这条**同时适用于 C 和 D 两节**。
5. **UTMOS 与"方言/口音"**：UTMOS 训练分布来自 Blizzard/VCC，**没有中文方言和歌唱**；对方言 TTS 它测的是"像不像录音室语音"，与"方言味"目标是**冲突**的——这正是你观察到"11 个信号层指标无法预测人耳偏好"的机制之一。

---

## 5. D. 唱歌语音转换（SVC）的评测指标

### D1. kNN-VC 原论文用了什么指标？（含"有没有歌唱实验"的确证）

论文：**Voice Conversion With Just Nearest Neighbors**，一作 **Matthew Baas**（与 Benjamin van Niekerk 等同贡献），**Herman Kamper**，Interspeech 2023，[arXiv:2305.18975](https://arxiv.org/abs/2305.18975)；代码 [github.com/bshall/knn-vc](https://github.com/bshall/knn-vc)。

**逐字核验的关键事实**：

- **❌ 论文中没有歌唱/歌声实验。** 全文实验仅在 **LibriSpeech dev/test-clean**（40 说话人 × ~8 分钟，16 kHz 英语语音）上完成；文末只提到 demo 页做了**跨语言、耳语、非语音、人→动物**的定性样例，**没有 VocalSet / NUS-48E / JVS-MuSiC / M4Singer 等歌唱数据集，也没有歌声指标**。（"Are complex methods still necessary…" 与 §4.1 实验设置）
  → 你要做"歌声 kNN-VC"，**没有现成的歌声评测先例可抄**，必须自建评测集与指标基线。
- **客观指标**：
  - 可懂度：**Whisper-base** 转写后算 **WER / CER**（论文用 WER，也报 CER）。
  - 说话人相似度：**x-vector 说话人验证系统的 EER**（越高越好，上限 50 %），x-vector 出处为 Snyder et al., ICASSP 2018。
  - 主观：MOS（1–5）+ SIM（1–4，沿用 VCC2020 协议）。
- **报告数字**（论文 Table 1，LibriSpeech test-clean 7800 条输出/模型）：

| 系统 | WER↓ | CER↓ | EER↑ | MOS↑ | SIM↑ |
|---|---|---|---|---|---|
| 测试集上界 | 5.96 | 2.38 | – | 4.24 | 3.19 |
| VQMIVC | 59.46 | 37.55 | 2.22 | 2.70 | 2.09 |
| YourTTS | 11.93 | 5.51 | 25.32 | 3.53 | 2.57 |
| FreeVC | 7.61 | 3.17 | 8.97 | 4.07 | 2.38 |
| **kNN-VC** | **7.36** | **2.96** | **37.15** | 4.03 | **2.91** |

- **工程参数（照抄即可）**：WavLM-Large **第 6 层**特征（1024 维，**每 20 ms 一帧**，16 kHz）；**k=4**，余弦距离；参考音频 ≥10 min 时可增大到 k≈20；HiFi-GAN V1（改为吃 1024 维特征、16 kHz、128 维 mel、10 ms hop / 64 ms Hann），在 LibriSpeech train-clean-100 上训练，**prematched 版本最佳**。
- **✅ 8 GB 显存的直接证据（原文原句）**："With these design choices, **inference with 8 minutes of reference audio on a consumer 8GB VRAM GPU is faster than real-time**." → 你的硬件**正是论文目标硬件**。
- **参考音频长度效应**：<30 s 时可懂度与相似度显著下降；**超过 ~5 分钟收益饱和**（README 与 §5.2）。
- **发布的 checkpoint**（README 已核验）：`prematch_g_02500000.pt`（WER 6.29 / CER 2.34 / EER 35.73）、`g_02500000.pt`（6.39 / 2.41 / 32.55）；一行加载：`torch.hub.load('bshall/knn-vc','knn_vc', prematched=True, trust_repo=True, pretrained=True)`。**依赖：Python ≥ 3.10、torch ≥ 2.0**（与你的 3.10 / 2.6.0 完全匹配）。

### D2. SVC 领域的权威结论：客观指标与主观相关性很弱

**The Singing Voice Conversion Challenge 2023**，一作 **Wen-Chin Huang**，Interspeech 2023，[arXiv:2306.14422](https://arxiv.org/abs/2306.14422)。

原文明句（摘要 + §1）：**"We also investigated whether existing objective measurements were able to predict perceptual performance, and found that only few of them could reach a significant correlation."**

其他可直接引用的结论：26 个提交系统；**top 系统在自然度上达到人类水平，但没有任何队伍在相似度上达到目标说话人水平**；cross-domain SVC（用说话语音训练、转换歌声）明显更难。数据集来自 **NUS-HLT Speak-Sing (NHSS)**；so-vits-svc 被描述为社区最流行项目（>15k stars）。

→ **工程含义**：歌声方向**不要指望单一客观指标替代听感**，必须做**指标组合 + 项目内标定**（用你自己的几条"金标准"样例回归出阈值）。

### D3. 标准客观指标：出处与要点

| 指标 | 出处（已核验的部分） | 计算要点 | 推荐实现 |
|---|---|---|---|
| **MCD**（Mel-Cepstral Distortion） | 度量本身可追溯到 **Kubichek, "Mel-cepstral distance measure for objective speech quality assessment", PACRIM 1993**（该引文出现在 SingMOS 参考文献 [1]，已核验） | 梅尔倒谱系数逐帧差的欧氏距离，通常做 **DTW 对齐**后取均值。业界通用形式 `MCD = (10/ln10)·√(2·mean((c1−c2)²))`（**⚠️ 本项目标注：该式未从原始论文取到逐字定义句，请以原文复核**） | `pysptk` 的 `mcd`；或自写（numpy 即可） |
| **F0 RMSE / F0 相关系数** | 广泛用于 VC/SVC；CoMoSVC 等用 DIO 提 F0（[arXiv:2401.01792](https://arxiv.org/abs/2401.01792)） | 需 **VUV 掩码**（仅比对浊音帧）后算 RMSE / Pearson r | torchcrepe / librosa / pyworld |
| **VUV error（清浊判错率）** | 名称在 VC 文献通行；**具体定义句未取到原文** | 浊/清判决不一致的帧比例 | 自写 |
| **RPA（Raw Pitch Accuracy）/ RCA（Raw Chroma Accuracy）** | CREPE 与 RMVPE 均以 RPA/RCA 为主指标：[CREPE, ICASSP 2018, arXiv:1802.06182](https://arxiv.org/abs/1802.06182)、[RMVPE, Interspeech 2023, arXiv:2306.15412](https://arxiv.org/abs/2306.15412)（RMVPE 原文："superiority … in terms of raw pitch accuracy (RPA) and raw chroma accuracy (RCA)"） | RPA：估计音高落在容差（常见 ±50 cent）内的**浊音帧**比例；RCA：允许八度错后仍正确 | mir_eval / 自写 |
| **FVE / F0GPE** | SC VALL-E 使用 "F0 voiced error (FVE)" 和 "F0 gross pitch error (F0GPE)"（[arXiv:2307.10550](https://arxiv.org/abs/2307.10550)） | 浊音判错 / 粗 pitch 误差 | 自写 |
| **SECS** | 见 §B1 | 目标音色相似度↑、源音色相似度↓（**必须同时报两个**） | 见 §B3 |
| **歌唱 MOS 预测** | **SingMOS**：Tang, Shi, Wu, Jin，[arXiv:2406.10911](https://arxiv.org/abs/2406.10911)。数据集 3,421 条中/日歌唱片段、**4.25 小时**、16 kHz、**21,530 条 MOS 标注**、平均 4.47 s；含 21 个 SVS、11 个 SVC、6 个声码器系统；许可 **CC BY-SA-NC 4.0**；baseline 代码 [nii-yamagishilab/mos-finetune-ssl](https://github.com/nii-yamagishilab/mos-finetune-ssl)（1×3090Ti 微调 50 epoch） | 需在 SingMOS 上微调；论文发现加 F0/F0 方差**反而不提升** | 同上 |

### D4. 歌声 F0 提取算法推荐

| 算法 | 论文/仓库 | 说明 |
|---|---|---|
| **CREPE** | Kim, Salamon, Li, Bello, ICASSP 2018, [arXiv:1802.06182](https://arxiv.org/abs/1802.06182)；PyTorch 版 [torchcrepe](https://github.com/maxrmorrison/torchcrepe)（`pip install torchcrepe`） | `torchcrepe.predict(audio, sr, hop_length, fmin, fmax, model='tiny'|'full', batch_size=…, device='cuda:0')`；**返回 pitch 与 periodicity**；默认 **Viterbi 解码**（可避免倍频/半频错误）；提示"CREPE 未在静音上训练，可能给静音区高置信度"，需用 `threshold.Silence(-60.)` |
| **RMVPE** | Wei, Cao, Dan, Chen, **Interspeech 2023**, [arXiv:2306.15412](https://arxiv.org/abs/2306.15412)；代码 [github.com/Dream-High/RMVPE](https://github.com/Dream-High/RMVPE) | **直接从复音音乐提取人声音高**（不需先做音源分离），以 RPA/RCA 评估，对不同 SNR 稳健 → **歌唱场景首选** |
| **pYIN** | 在 CREPE 摘要中被列为当时最强 DSP 基线（"such as the pYIN algorithm"）；**pYIN 原始论文（Mauch & Dixon, ICASSP 2014）本次未抓取核验** → 「**未找到可靠出处**」 | librosa 内置 `librosa.pyin`，可作为对照 |
| **DIO** | 被 CoMoSVC 采用（Morise et al., 2009，经 CoMoSVC 参考文献核验） | 低成本对照 |
| **so-vits-svc 是否使用 RMVPE** | SVCC2023 与 CoMoSVC 均提及 so-vits-svc 项目（[github.com/svc-develop-team/so-vits-svc](https://github.com/svc-develop-team/so-vits-svc)），但**我未抓取该仓库核实其 F0 前端** → 「**未找到可靠出处**」 | — |

### D5. 怎么判"音高保住了、音色换掉了"

**建议的判定组合（推荐直接落地，依据来自 SVCC2023 的"多指标 + 项目内标定"结论）**：

1. **音高保持**：对**源歌声**与**转换后歌声**分别用 RMVPE 提 F0，按帧对齐（可 DTW）后计算
   - **VUV error**（浊音帧判决不一致率）—— 应 ≈0；
   - **VUV 掩码内的 F0 RMSE（cent 或 Hz）** 与 **F0 相关系数 r**；建议门槛：`r ≥ 0.95`、`VUV error ≤ 5%`（**本项目建议值，需用你的金标准样例标定**）；
   - **RPA（±50 cent 容差）**：越高越好。
2. **音色已换**：`SECS(转换音频, 目标音色参考) − SECS(转换音频, 源歌声)` 应为**显著正值**；同时报告与目标参考的绝对相似度（见 §B1 门槛）。
3. **内容未变**：对唱词用 ASR（中文用 paraformer-zh / Fun-ASR-Nano）算 CER；**注意**：歌唱的 ASR 误差天然偏高，因此**必须与"源歌声经同一 ASR 的 CER"相减取相对值**。
4. **自然度**：SingMOS 微调模型；**不要**直接套用语音 UTMOS。

---

## 6. E. 落地建议：分层验收协议（最重要）

### E1. 通用原则

1. **每一层都要有"真人参考"基线**：同一条测试文本/旋律，用**真人方言/真人唱歌/真人同说话人**跑同一套指标，验收看**相对差**，不看绝对值。
2. **身份类指标（方言/音色）与内容类指标（CER）分离**，不得互相替代。
3. **阈值必须在项目内标定一次**（因为 SECS/DEC 都依赖嵌入模型）。

### E2. ① 方言合成（DiaMoE-TTS）

| 层 | 指标 | 怎么算 | 阈值建议（依据） |
|---|---|---|---|
| L1 内容正确性 | **方言 ASR 的 CER**（Fun-ASR-Nano 或 SenseVoiceSmall；中文普通话可用 paraformer-zh 做对照） | 合成音频 → ASR → 与目标文本算 CER | **不要设绝对值**：要求 `CER(合成) ≤ CER(真人方言参考音) + Δ`，Δ 建议 **+5~8 个百分点**（本项目建议）。✅ 已确认：**Whisper-large-v3 在中文方言上 CER 高达 31–88 %**，绝不能用它当方言内容裁判 |
| L2 **方言身份（核心）** | **(a) DID 分类准确率 DCA**；(b) **方言嵌入余弦相似度 DECS** | (a) 自训 DID 分类器对每条合成音频预测 top-1，统计"预测=目标方言"的比例，并与真人方言音的同指标对比；(b) 用同一骨干提方言嵌入，与"目标方言真人嵌入中心"求余弦 | (a) **相对**：`DCA(合成) ≥ DCA(真人) − 10pp`（本项目建议，参照 KeSpeech 6 类基线上限 78 %、ECAPA 基线 60.77 %）；(b) **绝对**：**DECS ≥ 0.8**（**文献阈值**，TMD-TTS 数据流水线所用，[arXiv:2509.18060](https://arxiv.org/abs/2509.18060)） |
| L3 音色身份 | **SECS**（ERes2NetV2 w24s4ep4 + WavLM-Large-SV 双报） | 同 §B1 | 先测真人上界（约 0.73–0.75），要求 **≥ 上界 − 0.05**；绝对线 **≥0.70** |
| L4 自然度 | **UTMOS + DNSMOS(P.835 OVRL)** | 见 §C | UTMOS 只做**回归护栏**（相对历史版本不下降）；DNSMOS OVRL **≥3.0**（TMD-TTS 的增强触发线是 <2.7，健康样本多在 3.3–3.6） |
| L5 韵律保真 | 与参考音的**时长比 / 停顿时长分布 / F0 轮廓 DTW 距离** | 见 §D3、§D5 | ⚠️ 你已实测"能量方差、F0 标准差、谱平坦度、过零率"等 11 个信号层指标**无法预测人耳偏好**——建议**直接砍掉**这些，改为"对**同一方言的真人参考**做相对比较"，并把预算投到 L2 |

**针对"自动挑选规则与人耳一致率 11.1 %"的根因诊断（有文献支持）**：
- 你用 CER 做挑选 → 挑的是"最像普通话" → 与"方言味最足"负相关（Seed-TTS 原文结论，见 §1）。
- 多数信号层指标（能量方差、谱平坦度、ZCR）与**方言身份**无关，只反映**录音/发音风格**，因此与"哪个更像成都话"几乎不相关 → 名次集中在 1.06–1.78 属必然。
- **修正方向**：把挑选准则换成 **argmax(DID 目标方言后验)**，以 **DECS** 做 tie-break，用 CER 仅做**淘汰护栏**（CER 超过真人基线 +Δ 的直接弃用）。

### E3. ② 五语种配音（GPT-SoVITS）

| 层 | 指标 | 阈值建议 |
|---|---|---|
| L1 内容正确性 | **分语种 ASR 的 CER/WER**：zh→paraformer-zh；yue/ja/ko→SenseVoiceSmall（zh/en/ja/ko/yue）；en→Whisper-large-v3 | 逐语种设门槛（本项目建议：zh ≤5 %、en ≤4 %、ja/ko ≤8 %、yue ≤15 %，需用你真人录音标定） |
| L2 音色身份 | **SECS（目标=用户本人参考音）** | ≥ 真人上界 −0.05；绝对 ≥0.70（§B1） |
| L3 自然度 | **UTMOS + DNSMOS OVRL** | UTMOS 回归不降；DNSMOS OVRL ≥3.0 |
| L4 **语种身份**（配音特有） | **LID 准确率**（SenseVoiceSmall 的语种标签；zh/en/ja/ko/yue 五分类） | 每条合成音频的 LID 标签 = 文本标签，准确率 **≥95 %**（本项目建议）；这是**零训练即可得**的层 |
| L5 跨语种音色漂移 | 逐语种 SECS 的**极差 / 标准差** | 用户价值点是"同一音色说五语"，故应额外要求**跨语种 SECS 方差小**（本项目建议：max−min ≤ 0.05）→ **⚠ 该 0.05 已实测证伪，见 §E4-实测修正** |

### E4-实测修正（2026-10-07）：L5 的 0.05 低于该指标自身的分辨率下限

上表 L5 的 `max−min ≤ 0.05` 是**计划阶段的建议值，不是论文值**，实测后必须修正。
用同一套独立说话人模型（ERes2NetV2）测两份数据：

| 数据 | 成对 SECS | 极差 max−min |
| --- | --- | --- |
| **同语种**：五句普通话（同一音色、同一语言） | 0.8610–0.9251（均值 0.8906） | **0.0641** |
| **跨语种**：zh/yue/en/ja/ko 各一句 | 0.8050–0.9009（均值 0.8602） | **0.0959** |

**同语种基线 0.0641 已经高于 0.05 这条线**——也就是说，即使一个系统在五种语言之间
完全没有任何额外漂移，它也不可能低于 0.05。**该阈值不可达，属于设定错误，不是系统缺陷。**

修正后的判据（建议，待用户确认）：

1. **相对判据（主）**：`跨语种极差 ≤ 1.5 × 同语种基线`。当前实测 **1.50**，刚好在线上。
   这条判据自带分辨率下限，不会像绝对值那样在噪声里失效。
2. **绝对判据（辅）**：`跨语种 SECS 均值 ≥ 0.80`。当前 0.8602。
3. 原来的 `≤ 0.05` **保留在报告里并显示为未达标**，不删除——它是"计划值被实测推翻"的记录。

**同时确认了一个真实的语种效应**：跨语种极差是同语种基线的 **1.50 倍**，
均值也从 0.8906 降到 0.8602。所以语言确实带来额外漂移，只是幅度远小于原计划假设的严重程度。
最差一对是 `en-zh`（0.8050）。

### E4. ③ 歌声音色转换（kNN-VC）| 层 | 指标 | 阈值建议 |
|---|---|---|
| L1 内容/唱词 | ASR CER（**与源歌声同 ASR 的 CER 相减**） | 相对差 ≤ +5pp（本项目建议） |
| L2 音色身份 | **SECS(→目标)** ↑、**SECS(→源)** ↓ | 目标 ≥0.70；且 `SECS_target − SECS_source ≥ 0.15`（本项目建议，需标定） |
| L3 音高保真 | **RMVPE F0**：VUV error、F0 RMSE(cent)、F0 相关系数 r、RPA@50cent | `r ≥ 0.95`、`VUV error ≤ 5 %`（本项目建议） |
| L4 自然度 | **SingMOS 微调模型**（不可用语音 UTMOS 代替） | 相对基线不降；绝对分需用 SingMOS 校准 |
| L5 频谱（可选） | MCD | **不建议作为主指标**（文献明确 MCD 与感知弱相关）；仅在排查"发闷/沙哑"时参考 |

### E5. 哪些必须自训？成本估算（8 GB VRAM）

| 需要自训的东西 | 为什么 | 数据量 | 8 GB 可行 batch | 训练时长估算 |
|---|---|---|---|---|
| **中文方言 DID 分类器**（必须） | 无现成方言级模型（§A3） | KeSpeech 6 类官话子集；按 3–6 s 片段、每类 3–5 万条 → 总量约 **150–300 h**（≈17–35 GB @16k/16bit，**本项目估算**）。上海（吴语）需另找数据 | 骨干用 **CAM++(7.2M) / ECAPA-TDNN(20.8M) / ERes2NetV2(17.8M)**：batch 64–128 无压力（这些模型本身 <25M 参数）。若用 Whisper-small：**必须只训头或 LoRA**——文献实测 head-only 需 **7.06 GiB @ bs=128**，8 GB 下把 bs 降到 ≤32 即可；**全参微调需 76 GiB，8 GB 不可行**（[arXiv:2512.02074](https://arxiv.org/abs/2512.02074) Table 1） | 参考 H100 上 Whisper-small 全参微调 **16.58 min/epoch @bs=128**；3070 Ti 有效吞吐约为其 1/10–1/15 → 同类全参约 **2.5–4 h/epoch**（**本项目估算**）。**推荐路线：ECAPA-TDNN/CAM++ + AAM-softmax，预计 30–90 min/epoch，10–30 epoch 收敛 → 半天到 2 天**（本项目估算）。若走 Whisper + LoRA/head 路线，文献报告 **LST RF=8 仅需 20.39 GiB @bs=128**、准确率 78.00 %，8 GB 下缩小 batch 可跑 |
| **方言嵌入 / DECS 参照中心** | 需要目标方言的嵌入质心 | 复用上面的 DID 骨干（同一模型既出分类又出嵌入），每方言 **≥200 条**真人音频算中心即可 | 推理即可 | 分钟级 |
| **歌唱 MOS 预测器**（歌声方向必须） | 语音 MOS 模型不迁移到歌唱（SingMOS 原文） | **SingMOS**（3,421 条 / 4.25 h，CC BY-SA-NC 4.0）；论文基线 1×3090Ti 微调 **50 epoch** | wav2vec2.0-base / HuBERT-base 微调，8 GB 下 batch 8–16 可行 | 单卡 3090Ti 约数小时 → 3070 Ti 预计 **6–12 h**（**本项目估算**，含一次数据下载） |
| 其余全部 | 不需要自训 | — | — | 0 |

**"零训练能做到哪一步"清单**：
- ✅ 音色相似度（SECS，用 ERes2NetV2 / WavLM-SV）
- ✅ 自然度（UTMOS / DNSMOS / SQUIM）
- ✅ 语种识别（SenseVoiceSmall 的 zh/en/ja/ko/yue）
- ✅ 内容 CER（paraformer-zh / SenseVoiceSmall / Fun-ASR-Nano / Whisper）
- ✅ 音高保真（RMVPE / torchcrepe）
- ❌ **方言身份（DID/DECS）——唯一必须自训的核心项**
- ❌ 歌唱自然度（SingMOS 微调，或至少一次微调）

---

## 7. 8 GB 显存可行性总表

| 组件 | 规模（已核验） | 8 GB 推理 | 8 GB 训练 |
|---|---|---|---|
| CAM++ | 7.2 M | ✅ | ✅（DID/SV 微调均可） |
| ERes2NetV2 / ECAPA-TDNN | 17.8 M / 20.8 M | ✅ | ✅ |
| WavLM-Large（kNN-VC 编码器） | **原文明确 8 GB 消费级 GPU 实时** | ✅ | 不训练（冻结） |
| kNN-VC HiFi-GAN 声码器 | 论文模型 | ✅ | ⚠️ 若自训需 LibriSpeech train-clean-100，训练强度大，**建议直接用官方 prematched checkpoint** |
| UTMOS / DNSMOS / SQUIM | 小 | ✅ | 不需要 |
| Whisper-small 全参微调 | 76 GiB @bs=128（文献） | — | ❌ 不可行；改 LoRA/head-only |
| SingMOS MOS 预测器微调 | wav2vec2-base 级 | ✅ | ✅（batch 8–16） |

---

## 8. 最小可行验收集（建议起步方案）

**先用这 4 个指标跑通闭环（全部零训练或有现成 checkpoint，1 天内可落地）：**

1. **SECS（ERes2NetV2-w24s4ep4 与 WavLM-Large-SV 双报）** — 覆盖②③的音色身份 + ①的音色保持。
   输入 16 kHz 单声道，输出 [-1,1] 余弦；门槛先测真人上界再定（预期 0.73–0.75，验收 ≥0.70）。
2. **DID 分类准确率 DCA（自训，唯一的"必须自训"项）** — 覆盖①的方言身份，**这是解决 11.1 % 一致率的唯一正确抓手**。
   参照：KeSpeech 6 类上，ECAPA-TDNN ~60.8 %、Whisper+LoRA ~78.8 %；再用 **DECS ≥ 0.8** 做二次筛选。
3. **分语种 ASR CER（paraformer-zh + SenseVoiceSmall）** — 覆盖①②③的内容正确性**护栏**（**不作为方言保真指标**）。
4. **UTMOS + DNSMOS(P.835 OVRL)** — 覆盖①②的自然度回归护栏（③另用 SingMOS 微调模型替换）。

**第 5 个（等你开始做歌声时再加）**：**RMVPE F0 的 VUV error + F0 相关系数**，配合 `SECS→目标 ↑ / SECS→源 ↓` 判"音高保住、音色换掉"。

---

## 9. 全部来源链接

**论文**
- KeSpeech: https://openreview.net/forum?id=b3Zoeq2sCLq ｜仓库 https://github.com/KeSpeech/KeSpeech
- DiaMoE-TTS: https://arxiv.org/abs/2509.22727
- DID / MEFT (Lin et al., APSIPA 2025): https://arxiv.org/abs/2512.02074
- Data2vec2+LLM 中文方言 ASR (TeleAI/NPU, 2025): https://arxiv.org/abs/2505.21138
- MMGER: https://arxiv.org/abs/2405.03152
- TMD-TTS（DCA/DECS 先例）: https://arxiv.org/abs/2509.18060
- FMSD-TTS（已撤稿版本）: https://arxiv.org/abs/2505.14351
- Bailing-TTS: https://arxiv.org/abs/2408.00284
- 阿拉伯语 ZS 方言 TTS: https://arxiv.org/abs/2406.16751
- NADI 2026: https://arxiv.org/abs/2609.27086
- Habibi（多方言 TTS benchmark）: https://arxiv.org/abs/2601.13802
- Cross-Dialect TTS (SLT 2024): https://arxiv.org/abs/2409.07265
- Blizzard 2020 上海话 TTS: https://arxiv.org/abs/2010.11489
- Seed-TTS: https://arxiv.org/abs/2406.02430 ｜评测集 https://github.com/BytedanceSpeech/seed-tts-eval
- VALL-E: https://arxiv.org/abs/2301.02111 ｜SC VALL-E: https://arxiv.org/abs/2307.10550
- NaturalSpeech 2（SIM-o/SIM-r 常引出处，定义句未取到）: https://arxiv.org/abs/2304.09116
- YourTTS: https://arxiv.org/abs/2112.02418 ｜ CosyVoice: https://arxiv.org/abs/2407.05407 ｜ CosyVoice 2: https://arxiv.org/abs/2412.10117 ｜ CosyVoice 3（9 语种 + 18 种中文方言）: https://arxiv.org/abs/2505.17589
- F5-TTS: https://arxiv.org/abs/2410.06885
- UTMOS: https://arxiv.org/abs/2204.02152 ｜仓库 https://github.com/sarulab-speech/UTMOS22
- Attacking UTMOS (SLT 2026): https://arxiv.org/abs/2606.31105
- DNSMOS (ICASSP 2021): https://arxiv.org/abs/2010.15258 ｜DNSMOS P.835 (ICASSP 2022): https://arxiv.org/abs/2110.01763 ｜实现 https://github.com/microsoft/DNS-Challenge
- NISQA: https://arxiv.org/abs/2104.09494 ｜仓库 https://github.com/gabrielmittag/NISQA
- TorchAudio-SQUIM: https://arxiv.org/abs/2304.01448
- kNN-VC: https://arxiv.org/abs/2305.18975 ｜仓库 https://github.com/bshall/knn-vc ｜demo https://bshall.github.io/knn-vc/
- SVCC 2023: https://arxiv.org/abs/2306.14422 ｜基线仓库 https://github.com/lesterphillip/SVCC23_FastSVC
- SingMOS: https://arxiv.org/abs/2406.10911 ｜baseline https://github.com/nii-yamagishilab/mos-finetune-ssl
- NeuCoSVC（kNN 式 SVC 先例）: https://arxiv.org/abs/2312.04919
- CoMoSVC: https://arxiv.org/abs/2401.01792 ｜ Robust One-Shot SVC: https://arxiv.org/abs/2210.11096 ｜ FreeSVC: https://arxiv.org/abs/2501.05586
- CREPE: https://arxiv.org/abs/1802.06182 ｜ torchcrepe https://github.com/maxrmorrison/torchcrepe
- RMVPE: https://arxiv.org/abs/2306.15412 ｜仓库 https://github.com/Dream-High/RMVPE
- ECAPA-TDNN: https://arxiv.org/abs/2005.07143
- WavLM: https://arxiv.org/abs/2110.13900 ｜ XLS-R: https://arxiv.org/abs/2111.09296（经 2512.02074 引文核验）
- VoxLingua107: https://arxiv.org/abs/2011.12998
- SenseVoice: https://arxiv.org/abs/2407.04051

**代码/模型库**
- FunASR Model Zoo: https://github.com/modelscope/FunASR ｜ `model_zoo/readme.md`
- 3D-Speaker: https://github.com/modelscope/3D-Speaker
- WeSpeaker: https://github.com/wenet-e2e/wespeaker（论文 https://arxiv.org/abs/2210.17016）
- so-vits-svc: https://github.com/svc-develop-team/so-vits-svc
- Whisper-DI-MEFT: https://github.com/linzr25/Whisper-DI-MEFT

---

## 10. 明确的「未找到可靠出处」清单

1. KeSpeech 的**压缩包/分片大小**（215 GB 是否够用）——官方只给百度网盘链接，未公布体积。
2. **公开可下载的四川话 / 上海话专用 ASR 模型**（含明确 CER 报告）。
3. **"UTMOSv2"的论文或官方仓库**（DiaMoE-TTS 表 2 使用了该名称）。
4. **NaturalSpeech 2 §4.4 中 SIM-o / SIM-r 的逐字定义句**（全文抓取在 §3.2 处被截断）。
5. **CommonLanguage-ECAPA（SpeechBrain）的标签空间**（HF 页面在本环境被网络阻断）；VoxLingua107 已核验为 107 语种。
6. **SpeechBrain `spkrec-ecapa-voxceleb` 的官方 EER 数值**（未抓到模型卡；已用 3D-Speaker / WeSpeaker 的 EER 表替代作为参数量与性能依据）。
7. **pYIN 原始论文（Mauch & Dixon 2014）** 与 **so-vits-svc 是否使用 RMVPE**。
8. **SQUID 的 arXiv ID / 官方仓库**（仅见于 SingMOS 参考文献列表）。
9. **WavLM-Large 的官方参数量**（仅按结构估算，已标注）。
10. **MCD 在 VC 场景下的逐字公式定义句**（度量源头 Kubichek 1993 已核验，公式为标准形式但未经原文逐字确认）。