# MVP 验证记录 · 清晰优先基线

> 日期：2026-10-17  
> 范围：20 句固定回归集、档位化发音层、参考音/文本配对、真实合成链路  
> 状态：通过首轮 MVP 验证；下一阶段需要 clear-dominant 数据，暂不进入平台化

---

## 1. 结论先行

这一轮验证只回答一个问题：

> **当前“清晰优先”基线能否稳定出音，并且能否用 CER + SECS 客观看出“口音变重但音色没变”的趋势？**

答案是：**能出音，也能测出问题。**

- 20/20 句成功合成，0 个可疑短音频，0 个削波；
- 清晰/技术句 CER 为 **3.12% / 0.00%**；
- 轻/中/浓广普 CER 依次为 **16.09% / 22.46% / 31.18%**；
- 同一批输出 SECS 稳定在 **0.78–0.81**，说明口音加重时“像不像”并没有同步恶化，**SECS 单独使用会得出错误结论**；
- 训练外叙述句出现真实误识：`纸鹤 → 紫鹤`、`雨后 → 以后`，该句 CER 12.5%；
- 受保护搭配（目的/的确/于是/可是/但是）在技术句中没有被全局替换破坏。

---

## 2. 环境与固定条件

| 项 | 值 |
|---|---|
| GPT 模型 | `GPT_SoVITS/pretrained_models/s1v3.ckpt` |
| SoVITS 模型 | `GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth` |
| 参考音 | `custom_data/sliced/训练.wav_0000018880_0000143680.wav` |
| 参考文本 | 今天天气真不错，我们出去走走吧。 |
| 默认口音档位 | `off` |
| 回归固定 seed | `20261017`（当前核心仍使用确定性种子，报告已记录 `effective=false`） |
| CER 模型 | SenseVoiceSmall，关闭 ITN，先去除 `<|...|>` 控制 token |
| SECS 模型 | ERes2NetV2，与合成引擎独立 |

固定样本：`backend/tests/fixtures/tts_regression_20.json`。共 5 组，每组 4 句：
`clear / technical / light / medium / heavy`。

---

## 3. 结果

### 3.1 分组指标

| 组 | 句数 | 成功 | 可疑短音频 | 削波 | CER 均值 | SECS 均值 | 时长均值 |
|---|---:|---:|---:|---:|---:|---:|---:|
| clear | 4 | 4 | 0 | 0 | 3.12% | 0.7601 | 3.21s |
| technical | 4 | 4 | 0 | 0 | 0.00% | 0.7680 | 5.92s |
| light | 4 | 4 | 0 | 0 | 16.09% | 0.7783 | 3.91s |
| medium | 4 | 4 | 0 | 0 | 22.46% | 0.7974 | 5.16s |
| heavy | 4 | 4 | 0 | 0 | 31.18% | 0.8091 | 4.51s |

全量：20 句，20 成功，CER 均值 14.57%，SECS 均值 0.7826，0 削波、0 可疑短音频。

### 3.2 关键观察

1. **清晰优先基线是可信的起点。**  
   4 句 clear 中 3 句 CER 0；technical 4 句全部 CER 0。说明底模 + 清晰参考音本身可以输出可懂的书面中文。

2. **SECS 不能单独当验收指标。**  
   口音从轻到浓，CER 从 16.09% 升到 31.18%，SECS 反而从 0.7783 升到 0.8091。浓口音让输出更像参考音，但更听不清。

3. **训练外文本仍是最薄弱处。**  
   `蓝色纸鹤停在窗台，雨后的街道很安静。` 被判成 `蓝色紫鹤停在窗台以后的街道很安静。`，CER 12.5%。这类同音/近音错误不能靠 SECS 发现。

4. **技术句和受保护搭配没有明显退化。**  
   `目的/的确/于是/但是/可是` 所在的技术句 CER 为 0。说明当前 `off` 基线和保护规则没有被旧的全量替换逻辑污染。

5. **口音档位的方向是正确的，但不应默认开启。**  
   轻/中档仍有明显可懂度损失；浓档只适合作为独立 accent/style 压力测试，不应进入默认生产路径。

### 3.3 同文本四档交换测试

为排除“不同组用了不同难度的句子”这个混杂因素，又做了一次固定文本、只改 `accent_level` 的交换测试：

> 你说这个方案的目的到底是什么？我们没有时间了，但是我们还是要验证。

| 档位 | CER | SECS |
|---|---:|---:|
| off | 3.33% | 0.8050 |
| light | 3.45% | 0.8012 |
| medium | 13.79% | 0.7791 |
| heavy | 20.69% | 0.7948 |

结论：在同一句话上，`medium/heavy` 的 CER 明显上升，而 SECS 仍稳定在 0.78–0.80。口音层确实在改变发音，而不是把音色换掉；同时也再次说明 SECS 不能单独验收。

复现：

```powershell
.\.venv\Scripts\python.exe scripts\accent_swap_verify.py
# 或复用已有音频
.\.venv\Scripts\python.exe scripts\accent_swap_verify.py --skip-synthesis
```

---

## 4. 本轮代码改动验证

### 4.1 档位化发音层

- 新增 `backend/services/pronunciation.py`；
- `off / light / medium / heavy` 四档；
- 长词优先；
- 固定搭配在渲染时保护，不是事后提示；
- `light/medium` 不做全局“的/是”替换；
- `heavy` 才做全局替换，但仍保护固定搭配；
- 旧 `tools.rules.to_lightmid()` 保留为 heavy 兼容入口；
- 旧 `AUTO_LIGHTMID=1` 映射为 `medium`，不再直接等价于全局替换；
- `PronunciationPlan` 已持久化：每句 output 带 `pronunciation`，输出目录写 `pronunciation.json`（含 `lexicon_id` / `accent_pack_id` / `accent_intensity` / `protected_terms` / 逐行 `replacements`）。

### 4.2 参考音/文本配对

- 新增 `backend/services/reference_prompt.py`；
- 先读 `sliced_corrected.list`，再读 whisper/raw 清单；
- 支持路径规范化、跨盘符文件名兜底；
- WebUI 选择已知切片时自动带出原文；
- 明显不匹配会在 `/api/synthesis` 前被拒绝；未知自定义音频只提示、不阻断。

### 4.3 回归台架

- 新增 `scripts/tts_regression.py`；
- 输出 `output/tts_regression/report.json`；
- 每条输出包含 duration、expected_duration、suspiciously_short、clipping、silence、CER、SECS；
- 评测失败不会丢弃整轮报告；
- 回归运行不会写入 WebUI 历史。

### 4.4 单一自然音频建档

输入原则已修正：用户只上传一段正常的自然说话音频，不需要照读台词本、不需要逐字填写参考文本、不需要补录特定口音。

新增 `backend/services/voice_ingest.py`：

- 自动 VAD、切片；
- 自动选 2–10 秒参考片段；
- 用本地 faster-whisper 自动转写；
- 跑参考音质量门禁；
- 返回 `ref_audio + ref_text`，WebUI 直接填入配置；
- ASR 有错字时文本仍可修正，但不再是默认前置步骤。

`POST /api/reference-audio` 默认 `auto_prepare=true`；原始上传保留，自动准备的参考片段另存，后续可继续用于音色 adapter。

后续已改异步：`POST /api/reference-audio/prepare` 秒回 job_id，切片/门禁/ASR 在后台线程跑，`GET /api/reference-audio/prepare/{job_id}` 按阶段轮询；同步接口保留兼容。

真实冒烟：

- 3.9 秒自然语音片段：自动选出 3.9 秒、ASR 得到“今天天气真不错，我们出去走走吧”、质量门禁 `ok`；
- 5 分 44 秒原始录音：自动选出 5.24 秒窗口、ASR 得到“还有13分钟就到了我今年二十几岁啊”、质量门禁 `ok`；
- 同步 `POST /api/reference-audio` 真实返回 200，`auto_prepared=true`；
- 异步 `POST /api/reference-audio/prepare` 真实返回 200，7.0 秒完成，前端无需用户填写原文。

---

## 5. 重要测量修正

这轮验证暴露了两个“尺子问题”，已修掉：

1. **SenseVoice 控制 token 计入 CER**  
   `<|zh|><|NEUTRAL|><|Speech|><|withitn|>` 不是语音内容。未清理时，完全正确的读回 CER 会从 0 变成 1.67。

2. **ASR ITN 把中文数字转成阿拉伯数字**  
   `三点` 被转成 `3点`，会被误算成合成错误。CER 评测现在关闭 ITN；从 `今天下午三点` 的样本看，CER 从 6.25% 回到 0。

---

## 6. 局限

- 只有一套音色权重，尚未做“换音色不换发音”的交换测试；
- SECS 只与当前参考切片比较，不是多人/多信道验证；
- 粤语/广普文本的 ASR 读回仍会受语种识别影响，必须配合人工抽听；
- 20 句只能证明方向，不足以证明生产可用；
- 当前 `seed` 字段已记录但尚未被核心真正读取。

---

## 7. 下一步

1. 保持 `accent_level=off` 作为默认生产路径；
2. 用一段已有自然说话音频跑 `voice_ingest` 自动建档；不要求用户照稿补录或补录口音；
3. 训练 clear-dominant 模型后重跑同一套 20 句；
4. 把同文本四档口音交换测试纳入固定回归，并补换音色、换信道测试；
5. ~~把 `PronunciationPlan` 持久化~~（已做：`pronunciation.json`）；下一步是接 `VoiceCard / AccentPack / StylePack` 与 license manifest；
6. TTS 可懂度稳定过线前，不进入 B2B API 平台化。

---

## 8. 复现命令

```powershell
# 全部后端测试
.\.venv\Scripts\python.exe -m unittest discover -s backend/tests -p "test_*.py"

# 前端测试与构建
cd frontend; npm test -- --run; npm run build; cd ..

# 20 句完整回归（含 CER/SECS）
.\.venv\Scripts\python.exe scripts\tts_regression.py

# 复用已有音频，只重算指标
.\.venv\Scripts\python.exe scripts\tts_regression.py --skip-synthesis

# 单句冒烟
.\.venv\Scripts\python.exe scripts\tts_regression.py --limit 1 --group clear
```
