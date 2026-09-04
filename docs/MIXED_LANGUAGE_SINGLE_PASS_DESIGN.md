# 五语种显式标签单次混合推理设计

**日期**：2026-08-29  
**状态**：当前基线已验证；后续以正常五语种混合为主线  
**适用模型**：当前仓库集成的 GPT-SoVITS v2/v2Pro 语言与音素链路

本设计的当前验收基线是 D 组 `s1v3.ckpt + v2Pro/s2Gv2Pro.pth` 的正常五语种混合结果，人工试听评分为 `7/10`。缩略词发音、EOS/semantic-token adapter 和音频拼接属于历史研究，不计入本阶段成功标准；基线与历史结论均汇总在 `archive/research/README.md`。

## 1. 目标与硬约束

目标是让一条台词中的 `zh`、`yue`、`en`、`ja`、`ko` 五种语言由显式标签控制，并在一次目标文本推理中生成一条连续音频。

这里的“单次混合生成”定义为：

```text
一次文本请求 -> 一次文本预处理 -> 一次 GPT/SoVITS 声学推理 -> 一个音频结果
```

语言片段可以在 G2P 阶段分别处理，但禁止在主路径中分别生成音频后再拼接。片段拼接仅允许作为明确标记的诊断实验，不得静默成为生产回退。当前主线暂不恢复任何缩略词拼接实验。

非目标：本阶段不修改模型权重、不自动翻译文本、不把单语言参考音频伪装成五语种参考音频，也不承诺突破模型上下文长度限制。

## 2. 当前实现证据

应用已经具备语言标签解析、脚本校验和五语种枚举：

- `backend/services/core.py` 的 `parse_language_segments()` 产生 `(language, text)` 片段。
- `render_language_segments()` 已能按片段跳过非中文广普转换。
- `GPT-SoVITS/GPT_SoVITS/TTS_infer_pack/TextPreprocessor.py` 已能把自动识别出的多个语言片段分别调用 `clean_text()`，再拼接 `phones` 和 BERT 特征。

当前缺口是 `SynthesisBackend.synth_segments()` 把显式片段重新拼成普通字符串，并选择 `auto`、`auto_yue` 或单语言模式。标签因此在引擎内部再次被 `LangSegmenter` 猜测覆盖。`LangSegmenter` 不能从同一批汉字可靠区分普通话和粤语，这也是显式 `<yue>` 仍可能读成普通话的根因。

## 3. 方案对比与选择

### 方案 A：显式片段级推理后拼接

每个片段单独调用声学模型，再做响度、速度和边界处理。听感兜底能力强，但不满足本项目严格的单次模型推理定义，主路径不采用。

### 方案 B：引擎内显式 G2P 编译，单次声学推理（采用）

输入网关保留标签，转换为结构化片段；`TextPreprocessor` 按片段调用对应语言 G2P，合并音素序列与 BERT 特征；`TTS.run()` 对合并结果只走一次推理循环。该方案复用现有语言模块，修改面集中，且能同时覆盖五种语言。

### 方案 C：为模型增加语言边界 token 或语言 embedding

理论控制力最强，但新增 token/embedding 会改变模型输入空间，通常需要重新训练或兼容性处理。本阶段不采用，作为后续模型训练课题。

## 4. 目标数据契约

应用内部使用 Segment IR，不再把语言控制编码回普通文本：

```python
Segment(
    text="我记得佢今日喺屋企",
    lang="yue",
    pause_after_ms=160,
    pronunciation_override=None,
    source_span=(8, 22),
)
```

必填字段为 `text` 和 `lang`；`lang` 只能是 `zh`、`yue`、`en`、`ja`、`ko`。未标记文本使用当前默认语言（默认 `zh`）。`source_span` 只用于调试，不能影响推理。

传给引擎的请求增加内部字段：

```python
{
    "text": "",  # 保留用于日志和兼容
    "text_lang": "segment_mixed",
    "text_segments": [
        {"lang": "zh", "text": "你好", "pause_after_ms": 160},
        {"lang": "en", "text": "Voice Lab", "pause_after_ms": 0},
    ],
}
```

`segment_mixed` 是引擎扩展模式，不向用户暴露为可选语言。缺少 `text_segments` 时维持旧有 `text_lang` 行为，保证普通单语言和兼容调用不变。

## 5. 单次推理数据流

```text
用户文本
  -> parse_language_segments()
  -> validate + 每语言规范化
  -> Segment IR
  -> TTS.run(text_lang="segment_mixed", text_segments=...)
  -> TextPreprocessor.preprocess_segments()
       -> 每段 clean_text(lang)
       -> 每段语言专用 G2P
       -> 每段 word2ph/BERT 对齐
       -> 插入已有标点停顿并合并 phones/bert/norm_text
  -> 生成一个 data item
  -> 一次 GPT 语义推理
  -> 一次 SoVITS 解码
  -> 一个采样率一致的音频
```

### 5.1 标签解析与规范化

- 继续复用现有 `<zh>...</zh>`、`<yue>...</yue>`、`<en>...</en>`、`<ja>...</ja>`、`<ko>...</ko>` 语法。
- 严格拒绝未闭合、交叉、嵌套、空内容和未知标签。
- 中文广普转换只对 `zh` 片段执行；`yue` 不执行普通话到广普的替换，避免破坏粤语字词。
- 英语规范化只作为词典入口，不再用 `A.I.` 这类可能改变重音的字符串替换作为最终控制手段。
- 日语、韩语、粤语片段在进入引擎前做脚本和可发音内容校验。

### 5.2 G2P 编译

新增显式编译函数，逻辑上等价于现有 `get_phones_and_bert()` 的合并部分，但语言列表完全来自 Segment IR：

1. 对每个片段调用 `clean_text_inf(segment.pronunciation_text, segment.lang, version)`；没有内部提示时回退到 `segment.text`。
2. 每段生成 `phones`、`word2ph`、`norm_text`；中文段生成 BERT 特征，其他语言按当前实现生成零 BERT 特征。
3. 对片段边界插入现有符号表支持的停顿标点（优先 `,`），不新增模型 token。
4. 拼接后强制检查 `len(phones) == bert.shape[-1]`，并记录每段起止 phone 下标。
5. 对 `pronunciation_override` 先查词/短语词典，再调用对应语言 G2P；词典输出必须经过符号表校验，禁止把未知音素直接送进模型。

首期不增加语言 embedding。语言控制来自显式选择的 G2P 和各语言已有音素集合；如果验收显示边界仍不稳定，再单独评估模型输入扩展和训练成本。

### 5.3 TTS 推理约束

- `TTS.run()` 识别 `text_lang == "segment_mixed"` 后只调用一次显式预处理，并生成一个 data item。
- 主路径关闭会把文本再次拆成多个 data item 的自动切句；超出上下文限制时直接报错，不能静默拆成多次声学推理。
- `parallel_infer` 只影响该一次推理的内部实现，不改变“调用次数为 1”的验收定义。
- `prompt_lang` 仍是参考音频文本的单一语言；目标文本混合不自动改变参考文本语言。
- 采样率、seed、温度、速度等参数沿用现有配置；同一请求只允许一个最终采样率。

## 6. 发音词典与 AI/缩写策略

发音问题分成两类处理：

1. **语言选择错误**：由 Segment IR 强制 `lang`，禁止交给 `LangSegmenter`。
2. **同一语言内部的词语读法错误**：由语言专用 pronunciation lexicon 处理。

词典项应记录 `language`、`surface`、`replacement` 或音素序列、来源和验收音频。`AI Voice`、`Wi-Fi` 等高频短语先作为词典候选，通过人工试听和 ASR 反向测试选择写法；不能假定添加句点一定改善重音。

词典匹配顺序为长短语 > 单词 > 正则模式，且只在对应语言片段内匹配。英文片段将表面文本与内部发音提示分离：`text` 保留用户输入，`pronunciation_text` 仅用于 G2P。纯大写首字母词默认按字母分读（如 `AI`、`API`、`GPT-4`、`URL`），常见可连读缩写通过例外词典保留整体读法（如 `NASA`、`NATO`、`UNESCO`）。词典失效时回退到该语言默认 G2P，不回退到自动语言识别。

## 7. 错误处理与兼容模式

生产默认模式为 `single_pass_explicit`：

- 标签错误、脚本不匹配、G2P 失败、phone/BERT 长度不一致、上下文超限，均返回可定位错误。
- 不得自动切换到 `auto`、`auto_yue` 或片段音频拼接。
- 现有无标签单语言请求继续使用旧路径。
- 为对比实验保留显式 `compat_auto` 开关，仅供开发诊断；其输出必须在日志中标记为非严格模式。

## 8. 测试与验收

### 8.1 组合矩阵

五种语言的全部有序组合为 325 组（单语言 5 组、两语 20 组、三语 60 组、四语 120 组、五语 120 组）。另外覆盖：

- 任意语言重复出现和相邻重复片段
- 语言切换前后有/无标点
- `AI`、`Voice`、数字、连字符和纯标点边界
- 短片段、长片段、全角标点、空白和注释行
- 错误标签、空标签、脚本不匹配和未知语言

### 8.2 必须通过的自动断言

- 每个混合请求的声学推理调用计数为 1。
- 每个片段的 `lang` 原样到达对应 G2P，测试中禁止调用 `LangSegmenter`。
- 合并后 `phones` 与 BERT 特征长度一致，且所有 phone ID 存在于当前版本符号表。
- 语言顺序、标点和片段边界索引保持不变。
- 上下文超限明确失败，不产生部分音频。

### 8.3 音频验收

每种语言至少准备一组正向和反向文本，保存输入、IR、规范化文本、phone 统计、模型参数和音频文件。使用人工试听加 ASR 反向校验：语言顺序正确、无整段语言漂移、无边界吞字、`AI Voice` 不出现 `A ivoice` 类错误。ASR 只用于筛选和回归，不作为唯一正确性来源。

## 9. 迁移、性能与回滚

- 第一阶段在应用和本地 GPT-SoVITS 副本中增加显式输入契约，原有单语言接口保持可用。
- 增加 `synthesis_mode` 日志字段，记录 `single_pass_explicit`、`legacy_auto` 或 `compat_auto`。
- 用同一 seed 对旧路径和新路径做 A/B 音频对比，记录耗时、phone 数和音频时长。
- 回滚只关闭显式模式路由并恢复旧调用，不删除标签解析、校验和词典文件。
- 不提交或覆盖用户的模型权重；引擎源码改动必须保留补丁文件，便于重新应用到新的 GPT-SoVITS 环境。

## 10. 参考实现位置

- 应用标签与任务入口：`backend/services/core.py`
- 引擎文本预处理：`GPT-SoVITS/GPT_SoVITS/TTS_infer_pack/TextPreprocessor.py`
- 引擎推理入口：`GPT-SoVITS/GPT_SoVITS/TTS_infer_pack/TTS.py`
- 语言自动分段（仅作兼容模式）：`GPT-SoVITS/GPT_SoVITS/text/LangSegmenter/langsegmenter.py`
- 语言 G2P 与符号表：`GPT-SoVITS/GPT_SoVITS/text/cleaner.py`、`chinese2.py`、`cantonese.py`、`english.py`、`japanese.py`、`korean.py`、`symbols2.py`
- 上游文本预处理参考：[GPT-SoVITS TextPreprocessor.py](https://github.com/RVC-Boss/GPT-SoVITS/blob/main/GPT_SoVITS/TTS_infer_pack/TextPreprocessor.py)
- 上游语言分段参考：[GPT-SoVITS LangSegmenter](https://github.com/RVC-Boss/GPT-SoVITS/blob/main/GPT_SoVITS/text/LangSegmenter/langsegmenter.py)
