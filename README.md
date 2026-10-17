# 像话 XiangHua · 角色声线平台

> 给独立游戏与短剧团队的“角色声线 API + 训练能力 + 授权账本”。
> 当前仓库是本地 WebUI/训练原型，正在向四层解耦架构与 B2B API 迁移。

---

## 当前状态（2026-10-17）

这是一次清场后的新起点：

- 已删除过期研究文档、旧实验/输出、缓存、DiaMoE 死路和旧中间 checkpoint；
- 保留用户原始录音、切片、ASR、最终权重、当前模型和运行环境；
- 默认合成已切到“清晰优先”：底模 v2Pro + 清晰参考音，关闭自动广普替换；
- 新架构与产品路线见：
  - [架构：音色/发音/韵律/口音四分离](docs/ARCHITECTURE.md)
  - [B2B 角色声线 API 与授权策略](docs/B2B_VOICE_API_STRATEGY.md)
  - [项目持久记忆](docs/PROJECT_MEMORY.md)

---

## 现在的核心原则

> **目标音频只用来提取音色；发音由字典决定；韵律和口音各自独立。**

不再默认对目标音频做全量 fine-tune。
全量微调会把音色、口音、韵律和内容纠缠在一起，造成“音色像但听不清”。

后续工作优先级：

1. 建固定回归集，先保证可懂度；
2. 把口音规则档位化，保护固定搭配；
3. 把发音字典从训练特征里外置；
4. 再训练音色 adapter；
5. 最后做 Managed Training、B2B API 和授权账本。

---

## 当前可运行内容

### 1. 本地 WebUI

```powershell
.\setup.ps1
& .venv\Scripts\python.exe tools\webui.py
```

打开浏览器后：

- 合成：粘贴台词 → 试听/下载；
- 批量：导入 TXT → 整批合成；
- 训练：切片、ASR、校对、特征、训练；
- 历史/设置：本机运行记录与模型路径。

### 2. 命令行批量合成

```powershell
& .venv\Scripts\python.exe tools\local_tts.py examples\台词示例_普通话输入.txt out\
```

可用环境变量覆盖：

```text
GPT_MODEL
SOVITS_MODEL
REF_AUDIO
REF_TEXT
AUTO_LIGHTMID
```

### 3. 当前默认模型

```text
GPT:    GPT-SoVITS/GPT_SoVITS/pretrained_models/s1v3.ckpt
SoVITS: GPT-SoVITS/GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth
参考音: GPT-SoVITS/custom_data/sliced/训练.wav_0000018880_0000143680.wav
文本:   今天天气真不错，我们出去走走吧。
```

这是“清晰优先”预设，不代表最终音色模型。

---

## 当前实现与目标形态

| 模块 | 当前 | 目标 |
|---|---|---|
| 合成 | 本地单用户 FastAPI + WebUI | 多租户 B2B API |
| 训练 | 本地 CLI/WebUI 训练链 | Managed / API / On-prem 三层 |
| 发音 | 文本替换 + G2P 补丁 | 外置 PronunciationPlan + 版本化字典 |
| 音色 | 参考音/全量微调 | speaker encoder + adapter |
| 口音 | 全局替换 | AccentPack + 强度档位 |
| 授权 | 文档与配置 | License Ledger + 证书 + 验证 |
| 导出 | 音频/历史 | WAV/MP3 + SRT + manifest |

---

## 文档

- [架构：四分离](docs/ARCHITECTURE.md)
- [B2B API 与授权策略](docs/B2B_VOICE_API_STRATEGY.md)
- [项目记忆](docs/PROJECT_MEMORY.md)
- [训练指南](docs/TRAINING_GUIDE.md)
- [使用手册](docs/USAGE_GUIDE.md)
- [WebUI 指南](docs/WEBUI_GUIDE.md)

---

## 目录结构

```text
backend/            FastAPI 后端、合成任务、声音/训练服务
frontend/           Vue 3 WebUI
tools/              本地合成、WebUI、训练封装
scripts/            训练链、数据集准备、验收脚本
GPT-SoVITS/         GPT-SoVITS v2Pro 引擎与模型
assets/             口音包清单、录音台词本、Logo
docs/               架构、产品、训练与使用文档
examples/           示例台词
patches/            发音补丁
third_party/knn-vc/ 当前音色转换链路
```

---

## 声音使用边界

- **只使用自己的声音，或已取得明确授权的配音演员/版权方声音。**
- 不做无授权声线爬取，不克隆动漫角色、公众人物或第三方配音演员。
- 商用输出必须有 `license_id`、授权范围和可验证记录。
- 声纹属于敏感个人信息；训练和商用前必须取得单独同意。

---

## License

MIT © 2026 MouYuc

GPT-SoVITS 本身为 MIT 协议，请遵守其许可条款。
