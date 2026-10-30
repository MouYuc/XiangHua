# IndexTTS2 作为第二引擎：接入评估

> 状态：评估完成，待用户确认后启动实验分支
> 日期：2026-10-25
> 背景：0h 决策拍板"评估接入 IndexTTS2"（GPT-SoVITS v2Pro 在情感/表现力上落后于开源竞品）

---

## 0. 结论先行

**有条件接入，先实验后商用**：

1. **技术上强烈契合**——IndexTTS2 的"情感与音色解耦 + 自然语言情绪描述 + 精确时长控制"正是本项目四层分离架构想要的韵律层能力，且时长可控对短剧字幕对齐是杀手锏（我们今天的 `episode.srt` 时间轴目前只能"量出来"，它能"定出来"）。
2. **商业上有一个硬闸门**——IndexTTS 代码仓库采用 **bilibili 自定义 Model Use License**：自托管与研究免费，**模型及其衍生产物的商业使用需要向 B 站注册并取得书面授权**（见 [LICENSE](https://github.com/index-tts/index-tts/blob/main/LICENSE) 与 [issue #228](https://github.com/index-tts/index-tts/issues/228)）。本项目规划的是 B2B 角色声线 API——**在拿到书面授权之前，IndexTTS2 只能进"本地实验/对比"路径，不能进对外产出路径**。
3. **建议路径**：两周实验分支跑通对比（见 §6 验收实验）→ 同时发起 B 站商用授权问询 → 授权到位则以可替换引擎形态并入 `SynthesisBackend`，不到位则把它的"情感指令 + 时长控制"思想留在产品契约里（`cfg.emotion` / `cfg.duration_hint` 字段先行），引擎侧等授权或换 CosyVoice（Apache-2.0，情感指令版）再落地。

## 1. 能力盘点

| 维度 | IndexTTS2 / 2.5（B 站 IndexTeam） | 对本项目的意义 |
|---|---|---|
| 开源时间 | 2.0：2025-09；2.5：2026-01（工业级） | 社区活跃、issue 响应快，生态成熟（ComfyUI/HF Space） |
| 许可证 | **bilibili 自定义 Model Use License**：自托管/研究免费；**商用需注册+书面授权** | B2B 输出的硬前提；与本项目 AGPL 自有代码不冲突但需双许可并存 |
| 显存 | 2.5 约 **6 GB**；2.0 Python 推理约 10 GB（WebUI 约 12 GB）；FP16 可再降 | 与 GPT-SoVITS v2Pro 同量级，双引擎不能同时驻留显存——GPU 租约必须管引擎切换 |
| 情感控制 | **情感与音色解耦**；自然语言情绪描述（"愤怒地说"）+ 参考音情感迁移；情绪强度可调 | 直接补上 StylePack 只有 speed/pause 的断档；与发音/口音层正交，映射干净 |
| 时长控制 | 自回归范式下**精确时长控制**（Speech Token Num 直接指定） | 短剧对齐刚需：台词对上时间轴不再靠重试 |
| 语言 | 中英为主，2.5 扩多语种（弱于本项目 5 语标记体系） | 混语标记 `<en>` 体系需要在新引擎侧重放 |
| 依赖 | Python 3.10–3.11；uv 管理环境；NVIDIA GPU；建议 CUDA 12.8+；git-lfs 拉模型（约 10 GB 磁盘） | 本项目 .venv 是 Python 3.10 + torch 2.6.0+cu126——**CUDA 12.6 vs 12.8 是共存风险点**，建议独立 venv 起步 |
| 性能 | 官方 A10 实测 2.5 比 2.0 RTF 提升 2.28×；消费卡表现依赖配置（3060 有慢速报告） | 需要在本项目 4090/卡型上实测单句耗时再下结论 |

来源：[index-tts/index-tts](https://github.com/index-tts/index-tts) · [IndexTeam/IndexTTS-2.5 模型卡](https://huggingface.co/IndexTeam/IndexTTS-2.5) · [技术报告](https://arxiv.org/html/2601.03888v3) · [商用授权 issue #228](https://github.com/index-tts/index-tts/issues/228) · [3060 性能 issue #585](https://github.com/index-tts/index-tts/issues/585)

## 2. 与四层架构的映射

| 层 | IndexTTS2 接入后的角色 |
|---|---|
| 音色层 | **被替换**：零样本参考音克隆由 IndexTTS2 承担（与我们 5–30 秒 quick_clone 前提一致）；`speaker_embedder` 变体 |
| 韵律层 | **被替换/增强**：情感指令 + 时长控制是它最强项；StylePack 扩展为 `{emotion, emotion_strength, duration_hint, speed, pause}` |
| 发音层 | **保留我们的**：`PronunciationPlan`（含新上的用户词典）在文本送引擎前完成改写，与引擎无关，原样复用 |
| 口音层 | **保留我们的**：广普四档是文本侧替换规则，继续在发音计划里生效 |
| 声线卡 | `timbre.encoder` 增加 `"indextts2-zeroshot"` 值；同一张卡记录两个引擎的参考音与参数默认值，版本语义不变 |

## 3. 集成方案草案

1. **引擎接口抽象**：把 `SynthesisBackend`（core.py:873）拆成 `EngineBackend` 协议（`ensure(cfg)` / `synth_one(cfg, text, cb, segments)` 现有签名），`GptSoVitsEngine` 与 `IndexTTSEngine` 同接口；`cfg["engine"] = "gpt_sovits_v2pro" | "indextts2"`，声线卡 defaults 落引擎字段。
2. **进程与路径隔离**：IndexTTS2 用**独立 venv + 子进程**（`subprocess` 列表参数管道 JSON 协议），不与 GPT-SoVITS 共享 sys.path——彻底规避两边 `tools` 包名冲突、`backend_cwd` 全局 chdir 竞争、以及 torch/CUDA 小版本冲突；代价是每句音频走管道（实测延迟可接受后再考虑进程内加载）。
3. **GPU 租约**：引擎切换 = 卸载旧引擎 + 加载新引擎，必须发生在 `GPU_ARBITER.lease("engine_swap")` 内；`/api/overview.gpu` 已能显示持有者，无需改契约。
4. **情感/时长进契约先行**：无论引擎何时落地，`SynthesisRequest` 先加 `emotion: str | None`、`emotion_strength: float | None`、`duration_hint_ms: int | None`，GPT-SoVITS 引擎忽略未知字段——API 面向未来稳定。

## 4. 工作量与风险（粗估）

| 步骤 | 估时 | 风险 |
|---|---|---|
| 独立 venv + 模型下载 + 官方 demo 跑通 | 1–2 天 | CUDA 12.8 vs 本机 12.6；uv/网络 |
| 子进程 JSON 协议封装（单句合成 + 情感/时长参数） | 2–3 天 | 协议细节、音频回传序列化 |
| EngineBackend 抽象重构 + cfg/声线卡扩展 + 回归对齐 | 3–4 天 | 现有 493 测试保持绿；seed 语义（IndexTTS2 也有种子？需核） |
| 20 句回归 + 交换套件双引擎对比 | 2 天 | 见 §5 |
| **商用授权问询**（与工程并行） | 未知时长 | **最大不确定项**：拿不到授权则只进实验路径 |

## 5. 验收实验设计

用现有 `scripts/tts_regression.py`（20 句固定回归 + 四档交换套件 + 换音色模式）双引擎各跑一轮，报告并排：

- **CER**（clear/technical/light/medium/heavy 五组，SenseVoice 读回）——IndexTTS2 的中文可懂度是否 ≥ GPT-SoVITS；
- **SECS**（说话人相似度，ERes2NetV2）——零样本克隆像不像；
- **情感表达**：新增 10 句"情绪台词"（愤怒/悲伤/惊喜各若干），双引擎各出自然语言情绪指令版与默认版，人工盲评 A/B；
- **时长控制精度**：`duration_hint` 设 3.0s，量实测时长误差分布（目标 |误差| < 150ms）；
- **单句耗时与显存峰值**（nvidia-smi 采样）：暖机后单句 P50/P95、6GB 声明是否成立；
- 交换测试：IndexTTS2 上重放"换音色不换发音"（--voice-card 双卡）验证四层解耦在新引擎上仍成立。

## 6. 与"自研韵律控制"的对比结论

| | 接入 IndexTTS2 | GPT-SoVITS 自研韵律 |
|---|---|---|
| 情感质量 | 即买即用，社区公认第一梯队 | 情绪参考音路线见效慢，且要自己追 |
| 时长控制 | 原生精确 | 无，需后处理变速（伤音质） |
| 许可 | **商用需 B 站书面授权** | 无外部约束（自有代码 + MIT 引擎） |
| 集成成本 | 独立 venv/子进程 + 引擎抽象（~2 周） | 长期投入，不确定性高 |
| 显存 | 双引擎不能同时驻留 | 单引擎无变化 |

**结论**：实验分支立即启动（质量对比是纯增益）；`emotion/duration_hint` 字段先落 API 契约；商用输出路径在拿到 B 站书面授权前不切换引擎，若授权不可得，备选方案是 Apache-2.0 的 CosyVoice-instruct（情感指令版）承接同一套契约。
