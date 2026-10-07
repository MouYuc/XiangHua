# 项目持久记忆

更新时间：2026-08-30

这份文档是后续会话的项目级记忆入口。处理本项目时，应先读取 `AGENTS.md` 和本文件，再检查本地知识图谱；不要因为没有专用 MCP 接口就直接声称“读不到知识图谱”。

## 本地知识图谱

知识图谱数据库位于：

```text
G:\Clone-youself\.code-review-graph\graph.db
```

这是 `code-review-graph` 自动生成的 SQLite 数据库，目录被 `.gitignore` 忽略，不应手工修改或提交数据库文件。图谱主要表：

- `metadata`：图谱版本、构建时间和对应 Git 提交
- `nodes`：文件、类、函数和测试节点
- `edges`：调用、导入和其他结构关系
- `communities`：模块社区
- `flows`：执行流
- `risk_index`：风险和测试覆盖索引

在没有图谱 MCP 工具时，使用只读 SQLite 查询访问它，例如：

```powershell
@'
import sqlite3
db = r"G:\Clone-youself\.code-review-graph\graph.db"
con = sqlite3.connect(db)
print(con.execute("select * from metadata").fetchall())
'@ | & G:\Clone-youself\.venv\Scripts\python.exe -
```

当前已知图谱元数据：

- `schema_version = 9`
- `postprocess_level = full`
- `last_updated = 2026-08-25T22:20:35`
- `git_head_sha = c2984f2c9c8b496d2d6fc16d2ce8f359e619de7f`

图谱可能落后于当前工作区。使用它定位结构关系后，必须回到当前源码和配置文件核验；不得把旧图谱内容当作当前代码事实。

## 当前架构事实

- 目标语言：中文、粤语、英语、日语、韩语。
- 用户只提供中文语料，系统学习中文说话人的音色、语气、语调和节奏。
- 用户文本使用显式 `<zh>`、`<yue>`、`<en>`、`<ja>`、`<ko>` 标签。
- 应用层生成 Segment IR，通过内部 `text_segments` 传入 GPT-SoVITS。
- 引擎使用 `text_lang = "segment_mixed"`，逐片段 G2P 后合并 phones/BERT 特征。
- 主路径必须是一次 GPT/SoVITS 声学推理生成一个连续音频，禁止拆分音频后拼接。
- 原始文本与内部 `pronunciation_text` 分离；不能把 `AI`、`A.I.`、`A I`、`API`、`GPT`、`URL`、`NASA` 等缩略词静默展开为完整单词。

## 当前模型谱系

实际推理默认加载的个人微调权重：

```text
GPT-SoVITS/GPT_weights_v2Pro/guangpu07-e14.ckpt
GPT-SoVITS/SoVITS_weights_v2Pro/guangpu07_e12_s396.pth
```

它们继承的正确 v2Pro 底模：

```text
GPT-SoVITS/GPT_SoVITS/pretrained_models/s1v3.ckpt
GPT-SoVITS/GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth
GPT-SoVITS/GPT_SoVITS/pretrained_models/v2Pro/s2Dv2Pro.pth
```

注意：`s1v3.ckpt` 虽然名称含 `v3`，但当前仓库的 `TTS.py` 明确把它配置为 `v2Pro` 的 T2S/GPT 底模；这是本项目模型侧消融实验的正确基础 GPT。

不得把下面这个标准 v2 GPT 当作当前 v2Pro 实验的基础 GPT：

```text
GPT-SoVITS/GPT_SoVITS/pretrained_models/gsv-v2final-pretrained/s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt
```

它属于标准 v2 模型家族，配套 `s2G2333k.pth`，可加载不等于适合作为 v2Pro 的受控对比候选。

辅助模型：

- `pretrained_models/chinese-hubert-base`
- `pretrained_models/chinese-roberta-wwm-ext-large`
- `pretrained_models/sv/pretrained_eres2netv2w24s4ep4.ckpt`

## 后续模型实验基线

固定同一中文参考音频、参考文本、输入文本、seed 和推理参数，比较：

1. 当前 GPT + 当前 SoVITS
2. `s1v3.ckpt` + 当前 SoVITS
3. 当前 GPT + `v2Pro/s2Gv2Pro.pth`
4. `s1v3.ckpt` + `v2Pro/s2Gv2Pro.pth`

记录漏读、提前 EOS、语义 token 数、音频时长、缩略词完整度、字母连读、人机感、五语种切换自然度和中文音色保持度。先完成模型路径/版本核验和实验计划，再修改生产代码。

## 个人音色转换（kNN-VC）

产品目标里的「音色唱歌」与「用我自己的音色配音」由此实现：把任意源音频（方言合成结果、
译制台词、歌声）搬到用户音色的 kNN-VC 转换。

- 上游代码：`third_party/knn-vc`（Baas, van Niekerk & Kamper, Interspeech 2023,
  arXiv:2305.18975）。**论文只做语音实验，没有歌唱实验**，所以歌声方向无先例可抄，
  必须自建基准与标定。
- 运行时：`scripts/knnvc_runtime.py`（本地检查点加载 + 分块编码 + 匹配池缓存）。
  **不要用 `third_party/knn-vc/hubconf.py`**：它走 `torch.hub.load_state_dict_from_url`，
  每次冷启动都联网下载；两个检查点已在 `third_party/knn-vc/checkpoints/` 下。
- CLI：`scripts/sing_convert.py`。
- 后端服务：`backend/services/timbre.py`，API 前缀 `/api/timbre`（profile / sources /
  convert / convert-file / status / jobs / unload）。

实测数据（RTX 3070 Ti 8GB）：

| 项 | 实测值 |
| --- | --- |
| 用户参考语料（`custom_data/sliced`） | 64 个文件 / 4.7 分钟 / 18.0 MB |
| 匹配合成池 | 11817 帧 / 236.3 秒，编码耗时 15.5s |
| 转换 2.5s 音频 | 0.1~0.2s |
| **模型显存占用** | **约 2.95 GB**（空闲 6.94GB → 3.99GB） |
| 分块窗口 | 15s/块（WavLM 全注意力，长音频会 OOM） |

注意：论文建议参考音 **<30s 明显退化、>5min 收益饱和**，且原文明写「8 分钟参考音在
消费级 8GB 显存 GPU 上比实时更快」——本项目硬件正是其目标硬件。

**方法学陷阱**：`knnvc_runtime.speaker_embedding_cosine()` 用的是**转换本身所用的
同一个 WavLM 第 6 层特征空间**，所以「输出与参考相似度高」是构造性必然，**不构成独立
证据**。测音色必须用独立的说话人验证模型。本机已有
`GPT_SoVITS/GPT_SoVITS/pretrained_models/sv/pretrained_eres2netv2w24s4ep4.ckpt`（102.5MB）
与其代码 `GPT_SoVITS/GPT_SoVITS/eres2net/`，**无需下载**。

## 客观评测（替代人耳盲听）

完整方案见 `docs/tts-objective-eval-playbook.md`（含论文出处与逐字引用）。四条必须记住的：

1. **「CER 方向反了」不是 bug**。Seed-TTS（arXiv:2406.02430 §3.1）明确：低 WER 通常意味着
   「更标准化、口音更弱」的语音。CER 只能当可懂度护栏，**绝不能当方言保真度指标**。
   这解释了 `DIALECT_PHASE1_BASELINE.md` 里自动挑选与人耳仅 11.1% 一致率的机制。
2. 方言真实性用 **DCA（方言分类准确率）+ DECS（方言嵌入余弦相似度）**，先例是
   TMD-TTS（arXiv:2509.18060），其 **DECS > 0.8** 阈值可直接引用。
3. **不存在零训练可用的中文方言级 DID 模型**（VoxLingua107 只有语种级；SenseVoiceSmall
   只到 `yue`）→ **方言判别器是唯一必须自训的核心项**。
4. 五语种配音可用零训练方案：**语种身份 LID（SenseVoiceSmall，zh/en/ja/ko/yue）≥95%**，
   以及用户价值点指标「**跨语种音色漂移：逐语种 SECS 的 max−min ≤ 0.05**」。

KeSpeech 的硬约束（若要训 DID 必须知道）：

- 规模 1542 小时 / 27237 说话人 / 8 个官话次方言，且含普通话与方言**平行录音**。
- **许可证：非商业 + 禁止演绎 + 禁止转分发**。有商业化预期就不要基于它训练并分发权重。
- **上海（吴语）不在其 8 类内**；且文献指出该数据集「方言味偏淡」，训出的判别器会系统性偏保守。
- 官方只给百度网盘下载；分片大小官方未公布（估算 ≈178GB PCM，215GB 属擦边）。

## 方言前端吞字：已修（2026-10-07）

完整实测见 `docs/DIALECT_LEXICON_DEFECTS.md` 第十三～十八节。四条必须记住的：

1. **量化吞字必须跑真实前端**，不能扫表。`hanzi2dialect_pinyin.py` 回退普通话拼音后
   `liandu_tone` 会改写调号（`zhe4`→`zhe7`），这个产物在任何表里都不存在；
   回退后**响不响**又取决于该方言 `syllable.xlsx` 有没有这个音节。工具：
   `scripts/frontend_loss_sweep.py`（158 句中文，按 `|` 切 chunk 实现**逐字归因**）。
2. **「缺字数」是上界不是损失量**。石家庄缺 3611 个常用字但实测 **0 丢字**（回退的普通话音节
   恰在其音系内）；青岛缺 1777 个同样 0 丢字。**只有上海实测有真实损失（131 音节 / 4.3%）**，
   其余 8 个方言（含普通话对照组）**方言特有损失为 0**。
3. **修法是普通话 IPA 兜底，不是编方言读音**。上海 `syllable.xlsx` 根本没有
   `zhe/mei/zhan/wei/shen` 音系族，编出来就是伪造。用厂商自带的
   `putonghua/base_syllable.xlsx` + `base_tone.xlsx` 把念不出的音节**改用普通话念出来**：
   **宁可念错，不可吞字**，且音节数不变 ⇒ F5 的时长预算不受影响。
   实现 `scripts/frontend_ipa_repair.py`，是**后处理**，不改 `third_party`；
   必须从第 2 段取原始普通话声调（变调会毁掉它），回填时校验 chunk 内容与 chunk 数，否则整行跳过并计数。
   实测：上海 182 行丢字 → 11 行（与对照组同水平），泄漏 131 → 0。
4. **`pinyin2ipa.py` 自己写的 `.log` 是现成的缺陷检测器**
   （`[Summary of pinyin mapping items not found]`）。本项目此前没有任何代码读它。

另外：`ipa_vocab_normalize.PUNCTUATION_ALIASES` 修掉了 `；`/`：` 被静默丢弃的问题
（两张表都没有，但它们是表停顿的，别名到模型已有的 `，`）；
括号引号是**刻意丢弃**（记在 `PUNCTUATION_DROPPED_ON_PURPOSE`）。这两处的唯一入口是
`ipa_vocab_normalize.normalize_for_model()`，生产路径与测量脚本共用，防止两边规则漂移。

**仍未解决**：上海那 131 个音节目前是普通话音，要变真方言只能补真实读音数据（数据获取仍受阻）。
**上线前的评测基线必须写明兜底率 ≈4.3%，不能宣称上海覆盖率 100%。**

## 工作边界

- Docker 已放弃，不恢复 Docker。
- 默认 Python：`G:\Clone-youself\.venv\Scripts\python.exe`。
- 模型运行前设置 `PYTHONIOENCODING=utf-8`。
- **保留工作区已有改动，不要丢弃。**

### Git 授权（2026-09-30 用户更新）

用户已明确授权：**允许按规定的中文格式提交到本地 `main`，但不允许 push。**

- 仍然**不要**执行 `git push`。
- 仍然**不要**执行 `git reset`、`git checkout --` 等破坏性/回退操作（除非用户明确要求）。
- 提交前先确认改动范围，并给出待提交清单。
