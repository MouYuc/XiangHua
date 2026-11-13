# 像话 B2B 角色声线 API 与授权策略

> 状态：产品与商业草案 v0.1
> 更新时间：2026-10-17
> 目标客户：独立游戏团队、短剧/剧情内容团队
> 说明：本文描述目标产品形态与落地路径，不代表当前代码已经实现；当前仓库仍是本地单用户 WebUI/API。
> 现行架构见 docs/ARCHITECTURE.md 与 docs/REFACTOR_2026-11.md。

---

## 0. 一页摘要

### 一句话定位

**像话不是“又一个 TTS”，而是给独立游戏和短剧团队使用的“角色声线 API + 授权账本”。**

客户得到的不是一段合成音频，而是：

- 一个可复用的角色声线；
- 一个可批量、可重做、可版本锁定的配音接口；
- 一条可商用、可审计、可验证的声音授权链。

### 三条产品主线

1. **Inference API**：把角色声线稳定地合成到几百/几千句台词。
2. **Training Capability**：让客户拥有自己的角色声线，而不是只能租平台预置声音。
3. **License Ledger**：把商用授权、声纹授权、项目范围和输出版本记录成可验证的账本。

### 首个楔子

先用 **短剧/剧情内容的批量多角色配音** 验证付费，再用 **独立游戏的 Unity/Unreal 集成与私有化** 做深护城河。

### 不做什么

- 不做通用“大而全”的 TTS 平台；
- 不与大厂正面竞争标准播音腔和通用多语言；
- 不做完整视频编辑器、口型同步或音乐生成；
- 不在没有付费客户前做声音 marketplace；
- 不把训练底层参数直接暴露给客户。

### 成功指标

- 首个付费 pilot 在 60 天内完成；
- 客户从提交台词到拿到可用音频 < 3 分钟；
- 每 100 句人工返工 < 10 句；
- 批量任务成功率 > 99%；
- 至少 3 个客户把角色声线用于真实项目；
- 训练评估通过率、授权转化率和复购率可被持续度量。

---

## 1. 市场判断与定位

### 1.1 为什么模型层已经商品化

市面上的 TTS 项目和大模型厂商已经证明：

- 标准普通话、英语、日语的合成质量会越来越接近；
- 模型权重、推理速度、音质都会快速商品化；
- 单靠“音质更好”很难建立长期差异。

对独立游戏和短剧团队来说，真正难的不是“能不能合成”，而是：

- 角色声线是否稳定；
- 几百句台词能否批量交付；
- 某一句不满意能否单独重做；
- 三个月后能否复现同一版本；
- 声音是否有清晰的商用授权；
- 数据能否不出内网；
- 成本是否可预测。

这些问题属于**工作流、资产和合规**，不是纯模型问题。

### 1.2 我们的差异点

| 维度 | 大厂通用 TTS | 开源 TTS 工具 | 像话的目标形态 |
|---|---|---|---|
| 核心卖点 | 标准、通用、云端 | 模型/脚本/可折腾 | 角色声线 + 口音 + 授权 |
| 口音 | 长尾，不一定覆盖 | 需要自己调 | 口音浓度可调，角色化 |
| 批量 | 有，但偏通用 | 需要自己写 | 台词本、角色、单句重做 |
| 训练 | 通常不开放 | 本地 CLI，门槛高 | Managed / API / On-prem 三层 |
| 授权 | 平台条款 | 自己判断 | 声纹授权 + 商用授权账本 |
| 部署 | 云端为主 | 本地 | 本地优先 + 云端可选 |
| 护城河 | 模型/算力 | 社区 | 角色资产 + 授权链 + 工作流 |

### 1.3 从“广普”扩展到“地域口音角色”

广普是第一个楔子，不是天花板。

建议路径：

1. 广普：团队已有 G2P 补丁、粤字词典、口音规则和训练经验。
2. 粤语、东北、川渝：复用 `accent_packs` 抽象，做口音包。
3. 地域角色配音：游戏 NPC、短剧角色、有声漫画、虚拟主播。
4. 角色声线资产与授权市场：在真实需求出现后再做。

---

## 2. 客户与场景

### 2.1 短剧/剧情内容团队

**典型任务**

- 一集 3–10 分钟，300–2000 句台词；
- 5–15 个角色；
- 需要快速试音、改词、重做单句；
- 需要 SRT/CSV 与音频一起交付；
- 需要明确的商用授权。

**核心痛点**

- 真人配音周期长、成本高；
- 多角色声线不统一；
- 改一句台词就要重新约棚；
- 通用 TTS 的“播音腔”没有角色感；
- 平台声音授权不透明。

**购买理由**

- 当天出样音；
- 整本台词批量生成；
- 单句重做不重跑全片；
- 口音浓度可调；
- 按项目授权，交付物可审计。

### 2.2 独立游戏团队

**典型任务**

- NPC 对话、战斗语音、环境旁白；
- 几百到几万句短句；
- 5–50 个角色；
- 需要 Unity/Unreal 集成；
- 需要版本锁定、离线/私有化、按角色授权。

**核心痛点**

- 角色声线在长周期开发中容易漂移；
- 每次改文案都要重新生成；
- 外包配音周期长、改稿贵；
- 声纹数据不能上传云端；
- 发行商/平台要求商用授权证明。

**购买理由**

- 角色声线版本可锁定；
- 批量 JSONL + Webhook；
- Unity/Unreal SDK；
- 本地/私有化部署；
- 按角色授权，授权链可验证。

### 2.3 建议的首发顺序

**建议：短剧先收钱，游戏做深护城河。**

理由：

- 短剧团队台词量大、交付急、决策快，适合 4–6 周跑出 case study；
- 游戏团队集成深、周期长，但一旦接入，切换成本高；
- 两者共用同一套 API、训练和授权底座，不需要做两个产品。

如果已有游戏团队 design partner，也可以先做游戏，但不要同时首发两个版本。

---

## 3. 产品结构

### 3.1 三层能力

```text
┌─────────────────────────────────────────────┐
│  客户界面：Console / API / SDK / 私有化       │
├─────────────────────────────────────────────┤
│  License Ledger  授权账本 / 证书 / 验证       │
├─────────────────────────────────────────────┤
│  Training        Managed / API / On-prem     │
├─────────────────────────────────────────────┤
│  Inference       角色声线 / 批量 / 单句重做    │
└─────────────────────────────────────────────┘
```

### 3.2 核心对象

| 对象 | 说明 | 关键字段 |
|---|---|---|
| Tenant | 客户组织 | tenant_id、套餐、配额、账单 |
| Project | 一个短剧/游戏项目 | project_id、类型、授权范围 |
| Role | 项目中的角色 | role_id、display_name、voice_id |
| Voice Card | 可复用的角色声线 | voice_id、version、accent、license |
| Voice Version | 不可变的声线版本 | version、recipe、dataset、evaluation |
| Dataset | 自动切片后的训练素材 | dataset_id、时长、质检报告、consent |
| Consent | 声纹所有者授权 | consent_id、身份、范围、期限 |
| License | 商用使用许可 | license_id、media、territory、term |
| Job | 批量合成/训练任务 | job_id、status、progress、outputs |
| Line | 单句台词 | line_id、role、text、accent、status |
| Asset | 输出音频/字幕/清单 | asset_id、url、duration、checksum |

### 3.3 客户旅程

```text
注册/开通
  → 创建项目
  → 上传一段自然说话音频（无需台词本）
  → 系统自动切片/ASR/选参考片段并创建角色声线
  → 上传台词本（JSONL/CSV/TXT）
  → 批量生成
  → 试听 / 单句重做
  → 导出音频 + SRT + manifest
  → 授权证书归档
```

### 3.4 产品模块

| 模块 | 面向谁 | 作用 |
|---|---|---|
| API | 技术团队 | 批量合成、训练、授权、用量 |
| Console | 制作/运营 | 项目管理、试听、重做、导出 |
| Voice Builder | 声音负责人 | 上传一段自然说话音频，自动切片/ASR、质检、训练、评估 |
| License Center | 制片/法务/发行 | 授权记录、证书、撤销 |
| Admin | 内部运营 | 租户、配额、审核、风控 |

---
## 4. API 设计

### 4.1 设计原则

1. **异步优先**：批量合成和训练都是长任务，不能阻塞 HTTP 请求。
2. **可重试**：每个写请求支持 `Idempotency-Key`。
3. **可复现**：输出 manifest 必须带 voice version、recipe、seed、license_id。
4. **可审计**：所有生成、训练、授权、撤销都写审计日志。
5. **租户隔离**：数据、文件、任务、用量都按 tenant 隔离。
6. **签名 URL**：音频不直接暴露本地路径，使用短期签名下载链接。
7. **Webhook 优先于轮询**：同时保留轮询接口，方便调试。
8. **只暴露产品概念**：`recipe`、`accent`、`project`，不暴露 `top_k`、`epoch` 等底层参数。

### 4.2 认证与租户

- `Authorization: Bearer <api_key>`
- API Key 只存哈希，支持 scope：
  - `voices:read`
  - `voices:write`
  - `jobs:write`
  - `training:write`
  - `licenses:read`
  - `licenses:write`
- 每个 Key 绑定 tenant、环境（test/live）、配额和速率限制。
- 生产 Key 与测试 Key 分离。

### 4.3 接口清单（MVP）

#### 角色声线

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/v1/voices/ingest` | 上传一段自然说话音频，自动切片、ASR、选参考片段 |
| POST | `/v1/voices` | 从已入库 VoiceAsset 创建角色声线（zero-shot 或提交训练） |
| GET | `/v1/voices` | 列出声线，可按授权/口音/语言筛选 |
| GET | `/v1/voices/{voice_id}` | 查看声线详情与版本 |
| DELETE | `/v1/voices/{voice_id}` | 撤销/删除声线 |

#### 项目与台词

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/v1/projects` | 创建项目 |
| GET | `/v1/projects` | 列出项目 |
| GET | `/v1/projects/{project_id}` | 项目详情 |
| POST | `/v1/projects/{project_id}/jobs` | 提交批量台词 |
| GET | `/v1/jobs/{job_id}` | 查询任务进度 |
| POST | `/v1/jobs/{job_id}/cancel` | 取消任务 |
| POST | `/v1/jobs/{job_id}/lines/{line_id}/retry` | 单句重做 |
| GET | `/v1/jobs/{job_id}/manifest` | 获取逐行结果与元数据 |

#### 训练

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/v1/datasets` | 上传自然说话音频，系统自动切片/转写 |
| GET | `/v1/datasets/{dataset_id}/report` | 数据质检报告 |
| POST | `/v1/voices/build` | 提交训练任务 |
| GET | `/v1/voices/build/{job_id}` | 查询训练进度 |
| GET | `/v1/voices/{voice_id}/evaluation` | 查看评估报告 |
| POST | `/v1/voices/{voice_id}/promote` | 评估通过后上线 |
| POST | `/v1/voices/{voice_id}/rollback` | 回滚到旧版本 |

#### 授权与用量

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/v1/licenses` | 创建商用授权记录 |
| GET | `/v1/licenses/{license_id}` | 查看授权 |
| GET | `/v1/licenses/{license_id}/certificate` | 下载证书（PDF/JSON） |
| GET | `/v1/licenses/verify/{license_id}` | 公开验证授权 |
| GET | `/v1/usage` | 查询用量与配额 |
| POST | `/v1/webhooks` | 配置回调 |

### 4.4 批量请求示例

```json
{
  "project_id": "proj_drama_001",
  "voice_id": "v_azhi",
  "voice_version": "2026-10-17.1",
  "license_id": "lic_001",
  "lines": [
    {
      "line_id": "L001",
      "role": "阿七",
      "text": "我系刺客村最靓嘅仔。",
      "accent": "medium",
      "emotion": "confident"
    },
    {
      "line_id": "L002",
      "role": "老板",
      "text": "你讲咩啊？",
      "accent": "light",
      "emotion": "confused"
    }
  ],
  "output": {
    "formats": ["wav", "mp3"],
    "sample_rate": 32000,
    "include_srt": true
  }
}
```

### 4.5 任务响应示例

```json
{
  "job_id": "job_001",
  "status": "running",
  "phase": "synthesis",
  "total": 2,
  "completed": 1,
  "created_at": "2026-10-17T12:00:00+08:00",
  "estimated_seconds": 18
}
```
### 4.6 Manifest 示例

```json
{
  "job_id": "job_001",
  "project_id": "proj_drama_001",
  "voice_id": "v_azhi",
  "voice_version": "2026-10-17.1",
  "license_id": "lic_001",
  "lines": [
    {
      "line_id": "L001",
      "role": "阿七",
      "text": "我系刺客村最靓嘅仔。",
      "audio_url": "https://.../L001.wav",
      "duration_ms": 2340,
      "sample_rate": 32000,
      "checksum": "sha256:...",
      "watermark_id": "wm_...",
      "status": "done"
    }
  ]
}
```

### 4.7 Webhook 事件

```text
job.created
job.progress
job.completed
job.failed
job.cancelled
dataset.validated
dataset.rejected
voice.training.progress
voice.training.failed
voice.ready
voice.revoked
license.created
license.revoked
```

### 4.8 错误模型

```json
{
  "error": {
    "code": "DATASET_TOO_SHORT",
    "message": "数据集有效语音不足 3 分钟。",
    "details": {
      "minimum_seconds": 180,
      "actual_seconds": 92
    },
    "request_id": "req_..."
  }
}
```

常见错误码：

- `UNAUTHORIZED`
- `FORBIDDEN_SCOPE`
- `RATE_LIMITED`
- `QUOTA_EXCEEDED`
- `DATASET_TOO_SHORT`
- `DATASET_NOT_SINGLE_SPEAKER`
- `DATASET_LOW_SNR`
- `TRANSCRIPT_MISMATCH`
- `TRAINING_FAILED`
- `EVALUATION_FAILED`
- `VOICE_VERSION_NOT_FOUND`
- `LICENSE_REQUIRED`
- `LICENSE_REVOKED`

### 4.9 SDK 顺序

1. REST/OpenAPI + Postman collection；
2. Python SDK；
3. Unity C# 示例；
4. Unreal 示例（后续）。

先不要做多语言 SDK 全家桶，先把 Python 和 Unity 做到能跑真实项目。

---
## 5. 训练能力

### 5.1 为什么训练能力必须放

只做推理 API，客户只能“租平台的声音”。

训练能力让客户：

- 拥有自己的角色声线；
- 复用同一声音做多季内容；
- 把口音、语气、角色设定沉淀成资产；
- 接受更高的客单价和更长的合同。

但训练能力不能裸放。它是最值钱、也最容易翻车的能力。

### 5.2 三层交付

| 层级 | 客户 | 交付方式 | 适合谁 |
|---|---|---|---|
| Managed Training | 大多数客户 | 客户上传一段自然说话音频，平台自动建档并代训 | 短剧团队、首次合作客户 |
| Training API | 审核 partner | 自助上传、训练、评估 | 有技术团队的工作室 |
| On-prem Training | 企业客户 | 本地 GPU 训练，数据不出内网 | 游戏/影视/有 NDA 的团队 |

### 5.3 训练配方（Recipe）

客户只选配方，不碰底层参数：

| Recipe | 输入 | 适用 | 说明 |
|---|---|---|---|
| `quick_clone` | 10–30 秒 | 快速试音 | 零样本/轻量适配，不训练或极少训练 |
| `standard_voice` | 一段 5–10 分钟自然说话音频（无需照稿） | 短剧/游戏常用 | 标准 s1+s2 微调，平衡质量与成本 |
| `studio_voice` | 30 分钟以上 | 主角/长期角色 | 更多数据、更多轮次、完整评估 |
| `accent_style` | 平台/授权口音包 | 口音风格包 | 与音色解耦；客户无需额外录口音素材 |
| `enterprise_custom` | 按合同 | 私有化/独家 | 专属训练、专属模型、SLA |

### 5.4 数据规范

最低要求（标准配方）：

- 用户只需提供一段自然说话音频；以下为系统自动质检后的建议，不是要求用户照稿补录；
- 优先单说话人；多人素材由系统自动聚类并选择目标说话人；
- 3–10 分钟有效语音；
- 16 kHz 以上，推荐 32 kHz/44.1 kHz；
- 单声道优先；
- 无背景音乐、无混响、无明显噪声；
- 削波 < 0.1%；
- SNR 建议 ≥ 20 dB；
- 系统自动 ASR；客户无需提供逐句转写，只有低置信度片段才可选复核；
- 每段 2–10 秒，避免过长；
- 不含他人声音、电视/音乐/环境人声。

高质量配方建议 10–30 分钟自然素材；若素材本身就覆盖以下维度更好，但不要求用户补录：

- 陈述句、疑问句、感叹句；
- 不同情绪；
- 不同语速；
- 角色常用口头禅；
- 口音由独立 accent pack 控制，不要求目标音频覆盖特定口音浓度。

### 5.5 训练流水线

```text
上传一段自然说话音频
  → 解压/格式检查
  → 自动 VAD / 说话人聚类
  → 音频质量检查
  → ASR/转写对齐
  → 切片/特征
  → s1 训练
  → s2 训练
  → 评估
  → 人工抽听（高风险/高价值客户）
  → promote 为 voice version
```

状态：

```text
validate → preprocess → features → s1 → s2 → evaluate → ready / failed
```

### 5.6 三道门禁

#### 数据门禁

不合格的数据直接退回，不要硬训：

- 时长不足；
- 多人声；
- 噪声/混响/音乐；
- 转写与音频严重不匹配；
- 采样率/声道异常；
- 削波严重。

#### 评估门禁

训练完必须跑固定测试集：

- CER（可懂度）；
- SECS（音色相似度）；
- 口音浓度是否符合目标；
- 固定 10–20 句人耳抽听；
- 与上一版本 A/B；
- 是否出现漏读、提前 EOS、异常长静音。

只有通过评估才能 `promote`。

#### 版本门禁

每次训练生成不可变版本：

```json
{
  "voice_id": "v_azhi",
  "version": "2026-10-17.1",
  "recipe": "standard_guangpu",
  "dataset_id": "ds_001",
  "evaluation_id": "eval_001",
  "license_id": "lic_001",
  "status": "ready"
}
```

项目合成时锁定 `voice_version`，避免模型被覆盖。

### 5.7 评估报告字段

| 字段 | 说明 |
|---|---|
| dataset_duration | 有效语音时长 |
| snr_db | 信噪比 |
| clipping_ratio | 削波比例 |
| transcript_cer | 转写与文本差异 |
| speaker_consistency | 声线一致性 |
| secs_to_reference | 与参考音相似度 |
| accent_match | 口音是否符合目标 |
| intelligibility_cer | 固定测试集 CER |
| human_review | 人耳抽听结论 |
| warnings | 风险提示 |
| verdict | pass / fail |

### 5.8 训练能力的商业化

- `quick_clone`：包含在基础套餐；
- `standard_voice`：按 voice build 收费；
- `studio_voice`：项目制收费；
- 额外修订：按次收费；
- 评估不通过：只收成本价或不计费（按合同）；
- On-prem：年度授权 + 支持费。

### 5.9 滥用与合规

训练能力必须绑定：

- 声纹所有者 consent；
- 身份/授权验证；
- 使用范围；
- 数据保留/删除策略；
- 高风险声音人工审核；
- 输出水印/来源元数据；
- 撤销与删除流程。

---
## 6. 商用授权与声纹授权

### 6.1 授权不是 PDF，是产品对象

每个 Voice Card 绑定一份或多份授权记录：

```json
{
  "license_id": "lic_001",
  "tenant_id": "tenant_001",
  "voice_id": "v_azhi",
  "consent_id": "consent_001",
  "media": ["game", "short_drama"],
  "territory": "CN",
  "term_start": "2026-10-17",
  "term_end": "2028-10-16",
  "exclusivity": "non_exclusive",
  "derivatives_allowed": true,
  "sublicense_allowed": false,
  "commercial_use": true,
  "status": "active"
}
```

### 6.2 授权范围字段

| 字段 | 说明 |
|---|---|
| media | 游戏、短剧、广告、有声书、直播等 |
| territory | 地域范围 |
| term | 起止时间 |
| exclusivity | 独家/非独家 |
| derivatives_allowed | 是否允许微调/改音色 |
| sublicense_allowed | 是否允许转授权 |
| commercial_use | 是否允许商用 |
| attribution | 是否需要署名 |
| compensation | 一次性/分成/年度费 |
| revocation | 撤销条件与善后 |
| data_retention | 原始音频保留/删除 |

### 6.3 授权徽章

声音列表直接显示：

- 已商用授权；
- 仅限本项目；
- 独家；
- 可转授权；
- 已撤销。

买家按徽章筛选，减少反复询问。

### 6.4 授权证书与验证

- `license_id` 写入每次输出的 manifest；
- 提供 PDF/JSON 证书；
- 提供公开验证接口：

```text
GET /v1/licenses/verify/{license_id}
```

返回：

```json
{
  "license_id": "lic_001",
  "voice_id": "v_azhi",
  "status": "active",
  "media": ["game"],
  "territory": "CN",
  "term_end": "2028-10-16",
  "issuer": "XiangHua",
  "signature": "..."
}
```

### 6.5 水印与来源

建议至少做两层：

1. **元数据/清单签名**：manifest 带 license_id、voice_version、checksum、生成时间；
2. **音频水印**：不可听或弱可听水印，用于溯源。

水印不是万能药，但能提高滥用成本，也是 B 端信任的一部分。

### 6.6 撤销与删除

- 声纹所有者撤销授权后，停止新的生成；
- 已交付且符合合同的项目按合同处理；
- 删除原始音频/模型/衍生数据；
- 记录审计日志；
- 通知相关 tenant。

### 6.7 法务清单（需律师审阅，不构成法律意见）

- 声纹属于敏感个人信息，需取得单独同意；
- 深度合成内容标识、生成式 AI 服务合规；
- 未成年人声音需监护人同意；
- 公众人物/第三方声音原则上不接；
- 训练授权与使用授权分开；
- 数据跨境、保留期限、删除机制；
- 商用范围、地域、期限、独家、转授权；
- 收益分配与违约责任；
- 侵权投诉与下架流程。

---
## 7. 定价与套餐（待验证）

> 以下价格仅用于 pilot 测试，不是最终定价。

### 7.1 Pilot 包

- 1 个项目；
- 1–2 个角色声线；
- 50–100 句真实台词；
- 2 周交付；
- 含一次训练/调优；
- 固定费用；
- 换取 case study 和反馈。

建议 pilot 价格：¥3,000–10,000/项目，视角色数量和训练量而定。

### 7.2 API 套餐

| 套餐 | 月费（示意） | 包含 | 适合 |
|---|---|---|---|
| Starter | ¥299–999 | 3 个声线、1 个项目、基础批量 | 小团队试水 |
| Studio | ¥2,000–10,000 | 20 个声线、团队席位、Webhook、优先队列 | 短剧/游戏工作室 |
| Enterprise | 定制 | 私有化、SLA、专属支持、On-prem | 中大型团队 |

超出套餐按字符/分钟计费；训练和授权单独计价。

### 7.3 训练与授权

| 项目 | 价格（示意） | 说明 |
|---|---|---|
| quick_clone | 包含 | 10–30 秒快速试音 |
| standard_voice | ¥1,999–4,999/个 | 一段 5–10 分钟自然说话音频（无需照稿） |
| studio_voice | ¥9,999–29,999/个 | 30 分钟以上，完整评估 |
| 额外修订 | 按次 | 评估不通过按合同 |
| 角色商用授权 | 一次性 + 年度 | 独家溢价 |
| On-prem 训练授权 | 年度授权 | 含支持/SLA |

### 7.4 定价原则

- 不要按“字数”单一计价，训练、授权、私有化都要单独收费；
- 用真人配音/录音棚成本做锚点；
- 首批客户可以折扣，但不要免费；
- 评估不通过要有明确的计费规则；
- GPU 成本和人工支持成本要进入报价。

---

## 8. Go-to-market

### 8.1 首发客户

- 短剧/剧情号制作团队；
- 独立游戏工作室；
- 有声漫画/广播剧团队；
- 本地生活商家的广普广告配音（后续）。

### 8.2 Design Partner

不要找 30 个客户。先找 3–5 个：

- 1 个短剧团队；
- 1 个独立游戏团队；
- 1 个有明确商用授权需求的团队；
- 1 个愿意提供真实台词和反馈的创作者。

给 design partner：

- 折扣；
- 优先支持；
- 参与产品设计；
- case study；
- 早期功能。

### 8.3 获客渠道

- 短剧制作群、MCN、制片人社区；
- 独立游戏社区、Game Jam、Steam/itch.io 开发者；
- B 站/抖音/小红书 demo 视频；
- GitHub/开源社区；
- 行业展会和线下开发者聚会；
- 通过声音演员/配音工作室合作。

### 8.4 内容素材

每条 demo 只讲一个场景：

- “同一句台词，四种广普浓度”；
- “两个角色吵架，一键生成”；
- “给游戏 NPC 配 500 句，不改一行代码”；
- “短剧改词不用重新约棚”；
- “你的声音，不出本机”。

### 8.5 销售材料

- 一页产品说明；
- 3 条 demo 视频；
- API 文档 + Postman；
- Unity 示例工程；
- 安全与合规说明；
- 授权证书样例；
- pilot 报价单；
- case study 模板。

---
## 9. 60/90 天路线图

### Phase 0：确认 design partner（第 0 周）

交付物：

- 1 个短剧或游戏团队；
- 50–100 句真实台词；
- 1–2 个角色声线需求；
- pilot 范围和价格。

验收：客户愿意提供真实素材，并接受付费 pilot。

### Phase 1：Managed Training + API 骨架（第 1–2 周）

交付物：

- 数据集上传与质检；
- 训练任务持久化；
- 评估报告；
- 角色声线 ID；
- API Key、租户、配额；
- 异步批量合成；
- manifest + 下载链接。

验收：真实台词端到端跑通。

### Phase 2：授权账本（第 3–4 周）

交付物：

- consent/license 数据模型；
- 授权证书；
- manifest 带 license_id；
- 撤销/删除流程；
- 用量记录。

验收：客户能拿到可验证的授权证书。

### Phase 3：Pilot 与 SDK（第 5–6 周）

交付物：

- Python SDK；
- Unity 示例；
- Webhook；
- 单句重做；
- pilot 报告；
- case study 草稿。

验收：客户在真实项目中完成一次交付。

### Phase 4：硬化与私有化（第 7–8 周）

交付物：

- Redis/RQ 或 Celery 队列；
- Postgres 控制面；
- S3/MinIO 存储；
- 重试/幂等/限流；
- Docker Compose；
- On-prem 安装包；
- 3 个付费客户。

验收：批量任务成功率 > 99%，可复现，可审计。

### 90 天目标

- 3 个付费客户；
- 1 个公开 case study；
- 1 套稳定 API + SDK；
- 1 套授权证书与验证流程；
- 明确的定价与 unit economics；
- 决定是否进入游戏 SDK/私有化第二阶段。

---
## 10. 技术架构与现状差距

### 10.1 当前可复用

- `backend/services/core.py`：合成核心、模型加载、参考音处理；
- `backend/services/synthesis_jobs.py`：异步任务与 SSE 模式；
- `backend/services/voice_selection.py`：声音档案/口音包抽象；
- `tools/train.py`、`run_dataset_prep.py`、`run_training_chain.py`、`verify_training_loop.py`：训练管线；
- WebUI：内部运营与调试台。

### 10.2 当前缺口

- 无 API Key/租户/配额/速率限制；
- 任务在内存 dict，重启丢失；
- 无持久化控制面；
- 无项目/角色/台词模型；
- 无 voice version/模型注册表；
- 无 license/consent 数据模型；
- 无授权证书/验证；
- 无水印/来源元数据；
- 无 Webhook；
- 无用量计量/账单；
- 无公开 API 文档/SDK；
- 无私有化打包；
- 单进程 GPU 模型加载，不适合多租户并发。

### 10.3 目标架构

```text
Client / SDK
    │
API Gateway（鉴权、限流、配额、审计）
    │
Control Plane（FastAPI + Postgres）
    ├── Tenant / Project / Role / Line
    ├── Voice / Voice Version / Dataset
    ├── License / Consent / Certificate
    └── Usage / Billing
    │
Job Queue（Redis + RQ/Celery）
    │
GPU Workers（合成 / 训练 / 评估）
    │
Object Storage（S3/MinIO，签名 URL）
```

### 10.4 建议技术选型

- API：FastAPI；
- 控制面：Postgres（pilot 可用 SQLite）；
- 队列：Redis + RQ（简单）或 Celery（复杂）；
- 存储：MinIO/S3；
- 部署：Docker Compose（pilot）→ Kubernetes（规模化）；
- 监控：结构化日志 + Prometheus/Grafana；
- 密钥：API Key 哈希存储，支持轮换；
- 文件：短期签名 URL，不暴露本地路径。

### 10.5 迁移原则

**包装，不重写。**

- 保留现有合成/训练核心；
- 在外部加 API 网关、任务队列、控制面和授权服务；
- 先做单租户 pilot，再做多租户；
- 先做 Docker Compose，再做 Kubernetes；
- 先做托管训练，再做自助训练。

### 10.6 非目标

- 不自研视频编辑器；
- 不自研口型同步；
- 不自研 billing 系统（pilot 用手工账单/Stripe/支付宝）；
- 不做通用模型市场；
- 不追求一次性支持所有语言/口音。

---
## 11. 指标与验证

### 11.1 北极星指标

**投入生产的授权角色声线数量**（Licensed voices in production）。

它同时反映：

- 客户真的在用；
- 训练/推理链路可用；
- 授权流程被接受；
- 有复购潜力。

### 11.2 关键指标

| 指标 | 目标 |
|---|---|
| 首次 API 调用时间 | < 10 分钟 |
| 从台词到可用音频 | < 3 分钟 |
| 批量任务成功率 | > 99% |
| 每 100 句人工返工 | < 10 句 |
| 训练数据质检通过率 | > 70% |
| 训练评估通过率 | > 80% |
| pilot 转付费率 | > 30% |
| 7 天/30 天留存 | 持续跟踪 |
| 授权证书使用率 | > 80% 项目 |
| 单分钟音频 GPU 成本 | 持续下降 |

### 11.3 Pilot 评分卡

| 维度 | 1 分 | 3 分 | 5 分 |
|---|---|---|---|
| 音质 | 不可用 | 可用但需修 | 可直接交付 |
| 角色一致性 | 漂移明显 | 少量漂移 | 稳定 |
| 口音控制 | 不可控 | 大致可控 | 精准可控 |
| 批量效率 | 很慢 | 可接受 | 显著快于人工 |
| 授权清晰度 | 不清楚 | 基本清楚 | 可审计 |
| 付费意愿 | 不愿 | 观望 | 愿意签合同 |

低于 3 分的维度，不要急着规模化。

---

## 12. 风险与应对

| 风险 | 影响 | 应对 |
|---|---|---|
| 声纹/深度伪造合规 | 高 | 单独同意、身份验证、水印、审计、律师审合同 |
| 训练数据质量差 | 高 | 数据门禁、质检报告、拒绝硬训 |
| 口音/音色漂移 | 中高 | 固定测试集、版本锁定、单句重做 |
| GPU 成本过高 | 中高 | 队列、缓存、批量、限流、按量计费 |
| 多租户数据泄露 | 高 | 租户隔离、加密、签名 URL、审计 |
| 客户不愿付费 | 中 | 先做 pilot、按项目收费、case study |
| 大厂推出类似功能 | 中 | 护城河放在授权链、角色资产、工作流、本地部署 |
| 支持成本过高 | 中 | Managed 优先、文档/SDK、限制自助训练 |
| 模型/框架升级 | 中 | 版本锁定、回归测试、灰度发布 |

---

## 13. 待决策问题

1. 首发客户选短剧还是独立游戏？
2. 训练先做 Managed 还是直接做自助 API？
3. 默认云端还是本地/私有化？
4. 角色声线资产归谁？客户、平台、还是共有？
5. 授权是“一次性 + 年度”还是“按项目 + 分成”？
6. 是否接受公众人物/第三方声音？建议原则上不接受。
7. 首发只做广普，还是同时上粤语/东北/川渝？
8. 是否把训练数据保留作为增值服务？
9. 是否提供 API 白标/代理？
10. 是否做水印？建议第一版就做元数据签名，水印第二阶段。

---
## 14. 附录

### A. 批量台词 JSONL 示例

```jsonl
{"line_id":"L001","role":"阿七","text":"我系刺客村最靓嘅仔。","accent":"medium","emotion":"confident"}
{"line_id":"L002","role":"老板","text":"你讲咩啊？","accent":"light","emotion":"confused"}
{"line_id":"L003","role":"阿七","text":"唔系啩？冇咁离谱啊嘛。","accent":"heavy","emotion":"surprised"}
```

### B. 数据集体检报告示例

```json
{
  "dataset_id": "ds_001",
  "duration_seconds": 412.5,
  "speaker_count": 1,
  "sample_rate": 32000,
  "channels": 1,
  "snr_db": 28.4,
  "clipping_ratio": 0.0003,
  "transcript_cer": 0.021,
  "verdict": "pass",
  "warnings": ["部分片段有轻微房间混响"]
}
```

### C. 授权记录示例

```json
{
  "license_id": "lic_001",
  "voice_id": "v_azhi",
  "consent_id": "consent_001",
  "tenant_id": "tenant_001",
  "media": ["short_drama"],
  "territory": "CN",
  "term_start": "2026-10-17",
  "term_end": "2027-10-16",
  "exclusivity": "non_exclusive",
  "commercial_use": true,
  "derivatives_allowed": false,
  "sublicense_allowed": false,
  "status": "active"
}
```

### D. 术语表

| 术语 | 含义 |
|---|---|
| Voice Card | 可复用的角色声线资产 |
| Voice Version | 不可变的声线版本 |
| Recipe | 训练配方，客户可见的产品概念 |
| Consent | 声纹所有者授权记录 |
| License | 商用使用许可 |
| Manifest | 逐行输出与元数据清单 |
| CER | 字错率，衡量可懂度 |
| SECS | 说话人嵌入余弦相似度，衡量音色相似度 |
| Managed Training | 平台代训 |
| On-prem | 客户内网私有化部署 |

### E. 相关文档

- `docs/PROJECT_MEMORY.md`：项目记忆与当前技术事实
- `docs/ARCHITECTURE.md`：音色/发音/韵律/口音四分离架构
- `docs/TRAINING_GUIDE.md`：当前训练管线与数据规范
- `docs/USAGE_GUIDE.md`：当前本地使用方式
- `docs/WEBUI_GUIDE.md`：当前 WebUI 能力

---

## 15. 下一步

如果这份策略成立，下一步不是继续加功能，而是：

1. 找到 1 个真实短剧团队或独立游戏团队；
2. 拿 50–100 句真实台词做 pilot；
3. 用 Managed Training 跑通一次角色声线；
4. 交付一次带授权证书的批量配音；
5. 用真实反馈决定 API、训练和授权哪些先产品化。

**先有真实客户，再扩平台。**
