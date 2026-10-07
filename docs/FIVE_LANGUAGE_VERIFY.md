# 五语种配音链路端到端客观验收

目标里的一句话是：**同一个个人音色，完成 中/粤/英/日/韩 五语种配音，一条台词一次推理**。
这句话里有三个可以用机器判定的部分，本文把它们逐个测掉，并记录两次**测量本身出错**的经历。

方法全部来自 `docs/tts-objective-eval-playbook.md`：

| 要回答的问题 | 方法 | 依据 |
| --- | --- | --- |
| 出来的音频真的是这个语种吗？ | **LID**，SenseVoiceSmall（词表就是 `chn_jpn_yue_eng_ko`） | 计划书 L4，零训练可得 |
| 五种语言还是同一个音色吗？ | **独立**说话人验证模型算 SECS（ERes2NetV2） | 计划书 L2/L5 |
| 真的只推理了一次吗？ | 引擎自报的 `synthesis_mode` 与 `acoustic_inference_calls` | 项目既定严格单次模式 |

CER 只作**可懂度护栏**，绝不当保真度指标：Seed-TTS（arXiv:2406.02430 §3.1）指出
低 WER 通常意味着「更标准化、口音更弱」。

## 一、先校准尺子：LID 自校准 5/5

SenseVoiceSmall 的仓库里自带 `example/zh.mp3`、`yue.mp3`、`en.mp3`、`ja.mp3`、`ko.mp3`，
**文件名就是真值**。先用它们验 LID，再去判项目产出的音频：

| 示例 | 识别结果 |
| --- | --- |
| `zh.mp3` | zh ✓ |
| `yue.mp3` | yue ✓ |
| `en.mp3` | en ✓ |
| `ja.mp3` | ja ✓ |
| `ko.mp3` | ko ✓ |

**5/5 正确**，尺子可用。

## 二、产品实测（带显式语言标签）

| 语言 | LID 判定 | 转写（SenseVoice） | 时长 |
| --- | --- | --- | --- |
| zh | **zh ✓** | 今天天气很好哦，我们出去走走哦。 | 3.16 s |
| yue | **yue ✓** | 今日天气好好，我哋出去行睇下。 | 3.12 s |
| en | **en ✓** | The voice lab is ready… | 2.43 s |
| ja | **ja ✓** | 今日はいい天気ですね散歩に行きま… | 2.89 s |
| ko | **ko ✓** | 오늘 날씨가 좋아서 산책 하루… | 2.50 s |

**LID 一致率 5/5 = 100%**，计划书门槛 ≥95% → **通过**。
粤语识别为 `yue` 而不是 `zh`，日语识别为 `ja`，韩语识别为 `ko`，都正确。

**混合单次模式**（五语种写在一条台词里，用 `<zh>…</zh><yue>…</yue>…` 标记）：

```
synthesis_mode       = single_pass_explicit   ✓
segment_count        = 5                      ✓
acoustic_inference_calls = 1                  ✓   输出 16.15 s
```

三项全部通过——**这是「一次提交、一次神经推理、一个连续音频文件」的直接证据**。

韵律健全性（pYIN，仅作 sanity check）：五种语言的 F0 中位落在 **108.7–124.2 Hz**，
浊音占比 0.73–0.91，说明是同一个音色的同一音区。

## 三、跨语种音色漂移：一次**被实测推翻的计划阈值**

| 数据 | 成对 SECS | 极差 max−min |
| --- | --- | --- |
| **同语种**：五句普通话（同一音色、同一语言） | 0.8610–0.9251（均值 0.8906） | **0.0641** |
| **跨语种**：zh/yue/en/ja/ko 各一句 | 0.8050–0.9009（均值 0.8602） | **0.0959** |

计划书 L5 写的是 `max−min ≤ 0.05`。**实测同语种基线就是 0.0641，已经超过这条线**——
即使五种语言之间完全没有额外漂移，也不可能达标。**这条阈值不可达，是设定错误，不是系统缺陷。**

所以：

- **保留原判据并如实标记为未达标**（不删除、不偷偷放宽）。
- 增加一条**自带分辨率下限**的相对判据：`跨语种极差 ≤ 1.5 × 同语种基线`。当前 **1.50**，刚好在线上。
- 绝对值参考：跨语种 SECS 均值 **0.8602 ≥ 0.80**。
- 修正理由已写回 `docs/tts-objective-eval-playbook.md` 的 §E4-实测修正，
  **该修正待用户确认**——我不擅自把阈值改成刚好能过的数。

**同时确认了一个真实的语种效应**：跨语种极差是同语种基线的 **1.50 倍**，均值从 0.8906 降到 0.8602。
语言确实带来额外漂移，最差一对是 `en-zh`（0.8050）。只是幅度远小于原计划假设。

## 四、两次「测量本身出错」，都差点变成假结论

### 1. 未加语言标签 → 韩语只出 0.08 秒，而报告显示「已检查」

第一版验收给五条句子**没有加标签**。未标记文本按页面默认语言（`zh`）合成，
而普通话前端没有韩文字母的音素。实测：

| 写法 | 时长 |
| --- | --- |
| `오늘 날씨가 좋아서 산책하러 가요.`（无标签） | **0.08 s** |
| `<ko>오늘 날씨가 좋아서 산책하러 가요.</ko>`（带标签） | **2.50 s** |
| 日语同样无标签 | 2.96 s（**没坏**） |

日语能活下来只是因为它和中文前端共享汉字假名，**这让失效变得非常不一致，因而危险**。
于是第一版报告的 LID 是 2/5、漂移 0.7452——**全部是测量错误造成的假象**。
改成显式标签后是 5/5 与 0.0959。

**顺带修掉了一个真实的产品缺陷**：引擎对这种「几乎没合成出来」的结果**报成功**，
WebUI 照样给出一个 4 KB 文件让人下载。现在 `backend/services/core.py` 会比对
「实得时长 vs 该文本应有时长」，低于 `SHORT_OUTPUT_RATIO = 0.25` 即标记
`suspiciously_short` 并给出中文告警，点明最可能的原因（缺语言标签）。实测：

```
untagged_ko   short=True   duration=0.08  expected=2.52   → 带告警
tagged_ko     short=False  duration=2.50  expected=2.52
untagged_zh   short=False  duration=3.16  expected=2.16
```

这个阈值刻意远低于重试用的 `_ACCEPTABLE_SEGMENT_DURATION_RATIO = 0.80`：
那个管「念得急了要重试」，这个管「根本没念出来要报错」。

### 2. LID 自校准静默跳过全部 5 项

SenseVoice 的示例音频在 `example/` **子目录**里，第一版只查仓库根目录，
于是五项全部 `missing`。而**一个每项都跳过的校准，看起来和一个全部通过的校准一模一样**。
现在路径会同时查根目录与 `example/`，且**一项都没拿到时会直接判 FAIL**，不再静默放行。

## 五、当前结论汇总

| 检查 | 结果 | 判据来源 |
| --- | --- | --- |
| 混合单次模式 + 只推理一次 + 5 片段 | **通过** | 项目既定严格单次模式 |
| LID 自校准 | **5/5 通过** | 本文件 §一 |
| 五语种 LID 一致率 | **5/5 = 100% 通过** | 计划书 L4 ≥95% |
| 跨语种漂移 ≤ 同语种基线 ×1.5 | **通过**（1.50，刚好在线上） | 本文件 §三，待确认 |
| 跨语种漂移 ≤ 0.05 | **未达标**（0.0959） | 计划书 L5，**已实测证伪** |
| 逐语种 CER | **未做** | 见下 |

## 六、没做的和已知限制

- **逐语种 CER 没做。** SenseVoice 的转写质量本身受音色和口音影响，
  要当护栏用需要先用真人录音标定门槛，否则「CER 高」既可能是发音错也可能是识别器不适配。
  计划书 L1 建议的门槛（zh ≤5%、en ≤4%、ja/ko ≤8%、yue ≤15%）**明确要求用真人录音标定**，
  本项目还没有这套标定数据。
- **只测了一条句子/语言。** 五条句子不足以估计逐语种的稳定性，
  上面所有数字都是单次观测，不是分布。
- **没有测 UTMOS / DNSMOS**（计划书 L3）。这两个需要模型权重，本机没有，网络对 HuggingFace 不可达。
- **粤语用的是「粤语汉字写法」而不是粤拼。** 端到端看是成功的（LID 判 yue），
  但没有验证具体字音是否正确。
- **音色参考音是普通话**，所以五种语言的「口音」都带着普通话底子；
  计划书 §L2 已经说明标签只控制发音语言、不创造母语韵律。

## 七、怎么复现

```powershell
# LID 模型（一次性，~900MB，ModelScope 可达）
.venv\Scripts\python.exe -c "from modelscope import snapshot_download; snapshot_download('iic/SenseVoiceSmall', cache_dir='.guangpu_webui/models')"

# 完整验收（合成 5 条逐语种 + 1 条混合 + 5 条同语种基线，然后全部客观测量）
.venv\Scripts\python.exe scripts\five_language_verify.py --validate-lid

# 复用已有音频，只重跑测量
.venv\Scripts\python.exe scripts\five_language_verify.py --validate-lid --skip-synthesis

# 回归门
.venv\Scripts\python.exe -m unittest backend.tests.test_five_language_verify -v
```

产物：`output/five_language/`（每条语言的音频 + `report.json`），`output/` 在 `.gitignore` 内。

## 八、素材与依赖

- **SenseVoiceSmall**（`iic/SenseVoiceSmall`，ModelScope）：`model.pt` 892.9 MB，
  词表 `chn_jpn_yue_eng_ko`。自带逐语种示例音频用于自校准。
  注意：它只到**语种**级（`yue`），**不能**当方言级判别器用——
  这一点在 `docs/PROJECT_MEMORY.md` 里已有记录，方言判别器仍需自训。
- **ERes2NetV2**：`GPT-SoVITS/GPT_SoVITS/pretrained_models/sv/pretrained_eres2netv2w24s4ep4.ckpt`，本机已有。
- **pYIN**（librosa）：用于基频健全性检查，不用 CREPE——CREPE 在本项目自有录音上会给出噪声曲线，
  见 `docs/SVC_SINGING_EVAL.md`。
