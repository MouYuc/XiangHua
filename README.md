# 像话 XiangHua · 角色声线平台

> 给独立游戏与短剧团队的“角色声线 API + 训练能力 + 授权账本”。
> 当前仓库是本地 WebUI/训练原型，正在向四层解耦架构与 B2B API 迁移。

---

## 当前状态（2026-10-17）

这是一次清场后的新起点：

- 已删除过期研究文档、旧实验/输出、缓存、DiaMoE 死路和旧中间 checkpoint；
- 保留用户原始录音、切片、ASR、最终权重、当前模型和运行环境；
- 默认合成已切到“清晰优先”：底模 v2Pro + 清晰参考音，默认 `accent_level=off`；
- 首轮 MVP 已跑通：20 句固定回归、档位化发音层、参考音/文本配对；
- 音色入口改为**一段自然说话音频**：系统自动切片、识别、选参考片段，用户不需要照读台词本或补录口音；
- 验证结果见 [MVP 验证记录](docs/MVP_VALIDATION.md)，新架构与产品路线见：
  - [架构：音色/发音/韵律/口音四分离](docs/ARCHITECTURE.md)
  - [B2B 角色声线 API 与授权策略](docs/B2B_VOICE_API_STRATEGY.md)
  - [项目持久记忆](docs/PROJECT_MEMORY.md)

---

## 现在的核心原则

> **目标音频只用来提取音色；发音由字典决定；韵律和口音各自独立。**

不再默认对目标音频做全量 fine-tune。
全量微调会把音色、口音、韵律和内容纠缠在一起，造成“音色像但听不清”。

后续工作优先级：

1. 固定回归集、档位化口音规则、参考音/文本配对：**已跑通首轮 MVP**；
2. 用一段已有的自然说话录音（建议 5–10 分钟）跑自动建档；不要求用户照稿补录；
3. 把发音字典从训练特征里彻底外置；
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
REF_TEXT            # 可选；WebUI 上传会自动识别
ACCENT_LEVEL=off|light|medium|heavy
AUTO_LIGHTMID=1   # 旧接口，映射为 medium
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
| 发音 | PronunciationPlan + 基础字典 | 版本化字典 + 训练特征外置 |
| 音色 | 一段自然音频自动建档 | speaker encoder + adapter |
| 口音 | off/light/medium/heavy + 固定搭配保护 | AccentPack + 强度档位 |
| 授权 | 文档与配置 | License Ledger + 证书 + 验证 |
| 导出 | 音频/历史 | WAV/MP3 + SRT + manifest |

---

## 文档

- [交接文档](docs/HANDOFF.md)
- [核心架构自检](docs/CORE_ARCHITECTURE_AUDIT.md)
- [/v1 API 契约](docs/API_V1.md)
- [MVP 验证记录](docs/MVP_VALIDATION.md)
- [架构：四分离](docs/ARCHITECTURE.md)
- [B2B API 与授权策略](docs/B2B_VOICE_API_STRATEGY.md)
- [项目记忆](docs/PROJECT_MEMORY.md)
- [训练指南](docs/TRAINING_GUIDE.md)
- [使用手册](docs/USAGE_GUIDE.md)
- [WebUI 指南](docs/WEBUI_GUIDE.md)

---

## API

- WebUI 用内部前缀 `/api/*`，随界面演进，不承诺稳定；
- 对外契约是 `/v1/*`，与 `/api` 同一套 handler 双挂载，只增不改；
- 交互式文档：`GET /docs`，机器可读：`GET /openapi.json`；
- 错误响应形如 `{"detail": "中文说明", "code": "NOT_FOUND"}`；
- 完整说明见 [/v1 API 契约](docs/API_V1.md)。

快速验证：

```powershell
curl.exe http://127.0.0.1:7860/v1/health
curl.exe http://127.0.0.1:7860/v1/jobs
```

### 安全与访问控制

服务默认只应绑定回环地址（`tools/api.py` 默认 `127.0.0.1`）。所有 `/api`、`/v1` 请求经过统一守卫中间件：Host/Origin 校验（防 DNS rebinding 与 CSRF）、`/v1` 可选 API Key、合成队列上限与请求体上限；`inspect`/`convert-file` 等带路径的接口只接受仓库目录内的路径。相关环境变量：

```text
XIANGHUA_API_KEY=<key>              # 设置后 /v1 需要 Bearer/X-API-Key（/api 供本机 WebUI 免鉴权）
XIANGHUA_API_KEYS_FILE=<path>       # 多租户模式：密钥注册表 JSON，经 /api/api-keys 管理租户 Key 与配额
XIANGHUA_ALLOWED_HOSTS=a,b          # 自定义允许的 Host（默认 localhost,127.0.0.1,[::1],testserver）
XIANGHUA_ALLOW_REMOTE_HOSTS=1       # 局域网部署时关闭 Host 校验（注意：/api 无鉴权，必须放在反向代理之后）
XIANGHUA_ALLOWED_PATH_ROOTS=G:\dir  # 为路径接口追加白名单根目录
XIANGHUA_ALLOW_PRONUNCIATION_PATCH=1  # 开启发音补丁导入（默认关闭；补丁只能改 GPT_SoVITS/text/）
XIANGHUA_MAX_MEDIA_BYTES=2147483648   # 单个媒体上传上限（默认 2 GiB）
XIANGHUA_SYNTHESIS_QUEUE_MAX=8        # 合成排队+运行任务上限，超出返回 429
XIANGHUA_SYNTHESIS_MAX_BODY_BYTES=1048576  # 合成请求体上限，超出返回 413
XIANGHUA_PROVENANCE=0               # 关闭每条输出的签名溯源 sidecar（默认开启）
```

详见 [docs/API_V1.md](docs/API_V1.md) §0；测试见 `backend/tests/test_security_guards.py`。

### 平台能力（2026-10-25 起）

- **整集交付**：`POST /api/batch/export` 把一次合成任务导出为 `episode.srt`（累计时间轴）+ 拼接整集音轨；工作台完成后出现「导出整集」按钮。
- **多角色对话编排**：`POST /api/dialogue/synthesis` 接受 `名字：台词` 剧本与「角色 → 声线卡」绑定，逐句合成、单句失败不中断；`POST /api/dialogue/export` 额外产出每角色分轨。
- **用户发音词典**：`/api/lexicon` 增删查「原词 → 读法」（如 重创 → 虫创），只改引擎读音不改字幕文本，优先级高于口音规则与固定搭配保护；设置弹窗内管理。
- **溯源凭据**：每条合成输出旁写 `<文件名>.provenance.json`（HMAC-SHA256 签名，含 license_id/consent_id/声线卡版本/文件哈希），`POST /api/provenance/verify` 校验；服务端密钥在 `.guangpu_webui/provenance.key`。
- **多租户 API Key**：`XIANGHUA_API_KEYS_FILE` 启用后经 `/api/api-keys` 创建/吊销租户 Key（token 只显示一次），每 Key 独立日配额与每分钟限速，`/v1` 按 Key 计量（含合成字符数）。
- **任务持久化**：合成任务快照落盘 `.guangpu_webui/jobs/`，重启后历史任务可见（中断任务标记为失败）。

---

## 目录结构

```text
backend/            FastAPI 后端、合成任务、声音/训练服务
frontend/           Vue 3 WebUI
tools/              本地合成、WebUI、训练封装
scripts/            训练链、数据集准备、验收脚本
GPT-SoVITS/         GPT-SoVITS v2Pro 引擎与模型
assets/             口音包清单、Logo（旧台词本仅作可选示例）
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

本项目自有代码采用 **GNU Affero General Public License v3.0（AGPL-3.0）**，全文见 [LICENSE](LICENSE)。

- 可以自由使用、修改、分发，包括商用；
- 如果把修改后的版本作为网络服务对外提供，必须同样以 AGPL-3.0 开放对应源代码；
- 如需闭源集成或商业托管豁免，可另行协商授权。

第三方组件的许可各自保留：GPT-SoVITS 为 MIT，kNN-VC 等按其仓库声明执行，不因本项目采用 AGPL 而改变。

© 2026 MouYuc
