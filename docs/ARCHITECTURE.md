# 像话新架构：音色、发音、韵律、口音四分离

> 状态：架构草案 v0.1
> 更新时间：2026-10-17
> 适用范围：所有后续 TTS、训练、Voice Builder、B2B API 与授权账本工作

---

## 1. 核心原则

一句话：

> **目标音频只用来提取音色；发音由字典决定；韵律和口音各自独立。**

当前 GPT-SoVITS 全量 fine-tune 会把“是谁”“怎么发音”“怎么说话”一起学进模型，导致：

- 音色相似度（SECS）很高；
- 但口音过重、可懂度下降；
- 改一个发音要重建特征/重训；
- 换一个口音会污染音色。

新架构必须把这几件事拆开。

---

## 2. 四层结构

```text
Text
  → 文本归一化
  → 发音层：G2P + 发音字典 + 多音字/口音规则
  → Phoneme Plan（音素、声调、边界、时长）
        ↓
  → 韵律层：停顿、重音、语调、情绪
        ↓
  → 声学骨干（冻结，多说话人训练）
        ↑
  → 音色层：speaker encoder / timbre adapter ← 目标音频
        ↑
  → 口音层：accent pack（音素替换、语调节奏）
        ↓
  → Decoder + Vocoder → 音频
```

四层各自只负责一件事：

| 层 | 只负责 | 数据来源 | 不应该学 |
|---|---|---|---|
| 音色层 | 说话人身份/音色 | 目标音频 | 口音、内容、韵律、信道 |
| 发音层 | 字→音素、多音字、口音读音 | 字典 + G2P | 音色、韵律 |
| 韵律层 | 停顿、重音、语调、情绪 | 多说话人标准语料/风格标签 | 目标说话人身份 |
| 口音层 | 音素替换、语调节奏 | 独立 accent pack | 音色 |

---

## 3. 数据契约

### 3.1 PronunciationPlan

发音层的唯一输出。模型不再直接吃“原始文本 + 隐式前端”。

```json
{
  "text": "这是用训练出来的音色合成的试音。",
  "language": "zh",
  "tokens": [
    {"text": "这", "phone": "zh e4", "tone": 4},
    {"text": "是", "phone": "sh i4", "tone": 4}
  ],
  "lexicon_id": "lex_zh_base_v1",
  "accent_pack_id": "guangpu_light",
  "accent_intensity": 0.35,
  "overrides": {
    "刺客": "ci4 ke4"
  }
}
```

要求：

- 可版本化；
- 可测试；
- 可人工修正；
- 不依赖目标音频；
- 改字典不需要重训音色。

### 3.2 VoiceCard

角色声线资产。只有音色部分来自目标音频。

```json
{
  "voice_id": "v_azhi",
  "display_name": "阿七",
  "timbre": {
    "encoder": "speaker_encoder_v1",
    "embedding": "embedding.bin",
    "adapter_version": "2026-10-17.1"
  },
  "default_pronunciation": {
    "lexicon_id": "lex_zh_base_v1"
  },
  "default_accent": {
    "pack_id": "guangpu_light",
    "intensity": 0.35
  },
  "default_style": {
    "preset": "natural"
  },
  "license_id": "lic_001",
  "status": "active"
}
```

### 3.3 AccentPack

口音包，独立于音色。

```json
{
  "accent_id": "guangpu_light",
  "language": "zh",
  "display_name": "轻广普",
  "lexicon": {
    "什么": "乜嘢",
    "没有": "冇",
    "是不是": "系唔系"
  },
  "protected_terms": ["目的", "的确", "于是", "可是", "但是"],
  "phone_rules": [],
  "prosody_profile": "guangpu_light_v1",
  "intensity_range": [0.0, 1.0]
}
```

### 3.4 StylePack

韵律/风格预设，不绑定音色。

```json
{
  "style_id": "lazy_confident",
  "display_name": "懒散自信",
  "pace": 0.95,
  "pitch_range": "medium",
  "energy": "low_medium",
  "pause_scale": 1.1,
  "emotion": "neutral"
}
```

### 3.5 VoiceAsset

授权音频入库记录。没有权利记录，不允许训练。

```json
{
  "asset_id": "asset_001",
  "source_type": "self_recorded",
  "source_id": "local_upload",
  "license_id": "lic_001",
  "consent_id": "consent_001",
  "territory": "CN",
  "term_end": "2028-12-31",
  "allowed_use": ["commercial_game"],
  "status": "approved"
}
```

---

## 4. 数据分工

### 4.1 音色数据

- 目标说话人的音频；
- 只训练 speaker encoder / timbre adapter；
- 需要覆盖不同音高、语速、情绪；
- 做信道/噪声增强，避免把麦克风和房间学进去；
- 用 speaker verification 目标，不用整段音频重建目标。

### 4.2 发音数据

- 文本 → 音素；
- 多音字、专有名词、缩写、口音读音；
- 与目标音频无关；
- 独立版本、独立测试、独立更新。

### 4.3 韵律/风格数据

- 多说话人、多风格标准语料；
- 只学停顿、重音、语调、情绪；
- 不绑定某个目标说话人。

### 4.4 口音数据

- 口音音素映射、声调习惯、语速/节奏；
- 独立 accent pack；
- 不进入音色模型。

---

## 5. 训练流水线

```text
授权音频入库
  → 数据质检
  → 说话人分离/聚类
  → 音色数据 / 发音数据 / 韵律数据 / 口音数据分流
  → 音色 encoder/adapter 训练
  → 发音字典构建
  → 韵律/口音包构建
  → 评估门禁
  → 发布 VoiceCard / Lexicon / AccentPack / StylePack
```

训练能力分三层：

1. Managed Training：平台代训；
2. Training API：审核 partner 自助；
3. On-prem Training：客户内网。

详细商业与授权设计见 `docs/B2B_VOICE_API_STRATEGY.md`。

---

## 6. 推理流水线

```text
Text
  → Normalizer
  → PronunciationPlan
  → ProsodyPlan
  → Acoustic backbone (frozen)
  + Timbre embedding (VoiceCard)
  + AccentPack
  + StylePack
  → Decoder/Vocoder
  → Audio + manifest
```

manifest 必须带：

```json
{
  "voice_id": "v_azhi",
  "voice_version": "2026-10-17.1",
  "lexicon_id": "lex_zh_base_v1",
  "accent_pack_id": "guangpu_light",
  "accent_intensity": 0.35,
  "style_id": "natural",
  "license_id": "lic_001",
  "checksum": "sha256:...",
  "watermark_id": "wm_..."
}
```

---

## 7. 评估：证明真的解耦

不要只看 SECS 或 CER 单一指标。至少做五个交换测试：

1. **换音色不换发音**
   同文本、同字典、不同音色 → CER 稳定。

2. **换字典不换音色**
   同音色、同文本、不同发音字典 → SECS 稳定。

3. **换口音不换音色**
   同音色、四档口音 → SECS 稳定，口音分类器/人耳判断变化。

4. **换信道不换人**
   同一人不同麦克风 → SECS 稳定。

5. **训练外文本**
   目标音频没出现过的文本 → CER 不崩。

建议指标：

- CER：可懂度；
- SECS：音色相似度；
- 口音分类/人耳评分；
- F0/时长/停顿统计；
- 削波、异常静音、提前 EOS；
- 人工抽听。

---

## 8. 从当前 GPT-SoVITS 迁移

### 阶段 0：清晰优先（已完成配置）

- 底模 v2Pro + 清晰参考音；
- 关闭自动广普替换；
- 先保证可懂度；
- 不重训。

### 阶段 1：发音层外置

- 把 `tools/rules.py` 改成 `off / light / medium / heavy`；
- 建立 `PronunciationPlan`；
- 建立版本化字典；
- 保护固定搭配；
- 先写测试，再改实现。

### 阶段 2：音色适配器

- 冻结内容/韵律骨干；
- 只训练 speaker encoder / adapter；
- 目标音频只参与音色目标；
- 加增强/对抗，减少口音、信道、韵律泄漏。

### 阶段 3：强解耦模型

- 内容编码器：音素序列 → 内容表示；
- 音色编码器：目标音频 → 说话人嵌入；
- 韵律/口音控制器独立；
- Decoder/Vocoder 合成。

---

## 9. 当前代码映射

| 当前文件 | 角色 | 新架构中的处理 |
|---|---|---|
| `backend/services/core.py` | 合成核心/模型加载 | 保留，外面包 PronunciationPlan |
| `backend/services/voice_selection.py` | 声音档案/口音包 | 升级为 VoiceCard/AccentPack |
| `tools/rules.py` | 文本替换 | 改为档位化、保护固定搭配 |
| `GPT_SoVITS/text/chinese2.py` | G2P 补丁 | 逐步外置到字典，不再依赖重训 |
| `scripts/run_dataset_prep.py` | 特征准备 | 保留，增加数据分流 |
| `scripts/run_training_chain.py` | s1/s2 训练 | 改为 recipe/Managed Training |
| `scripts/verify_training_loop.py` | 训练验收 | 增加解耦交换测试 |
| `backend/services/reference_audio.py` | 参考音质检 | 保留，更新阈值与注释 |
| `third_party/knn-vc/` | 音色转换 | 暂留，作为 timbre 候选/对照 |

---

## 10. 非目标与边界

- 不做无授权声线爬取；
- 不把目标音频用于发音/韵律训练；
- 不做全量 fine-tune 作为默认路径；
- 不在 TTS 可懂度过线前做 API 平台化；
- 不先做 marketplace；
- 不做完整视频编辑器/口型同步。

---

## 11. 下一步

1. 建 20 句回归集；
2. 写 `PronunciationPlan` 与档位化口音规则；
3. 给参考音/文本加配对校验；
4. 跑清晰优先基线；
5. 再决定音色 adapter 的具体模型路线。
