# 五语种单次混合推理实施计划

**文档状态**：当前基线记录与实现边界（2026-08-30）

**当前范围**：本轮只完成显式 Segment IR、单次推理契约、GPT-SoVITS 补丁和技术文档；Windows/Linux/macOS 安装入口与 Docker 集成暂缓，不作为本轮完成条件。

**当前效果基线**：D 组 `s1v3.ckpt + v2Pro/s2Gv2Pro.pth` 的不含缩略词五语种混合已完成试听，评分 `7/10`。缩略词拼接、EOS 调节、phone prosody adapter 和 semantic-token residual 已归档，不再作为本计划的当前验收项。

| 模块 | 状态 | 说明 |
|---|---|---|
| 应用层 Segment IR 与请求契约 | 已完成 | 混合片段按原顺序以 `text_segments` 传入一次请求 |
| GPT-SoVITS 显式 G2P 补丁 | 已完成 | 通过独立、可跟踪、幂等的补丁脚本注入 |
| 任务快照元数据 | 已完成 | 输出 `synthesis_mode`、`segment_count`、`acoustic_inference_calls` |
| 五语种正常混合基线 | 已完成 | 5 条 D 组样本均为一次 `segment_mixed` 推理，自动指标正常，人工评分 `7/10` |
| 五语种完整矩阵与边界验收 | 待后续 | 仍需补齐 325 组有序组合和引擎级边界测试 |
| 安装入口与 Docker | 暂缓 | 不属于本轮交付范围 |

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` or `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 本计划不包含 `git commit` 或 `git push`；按仓库 `AGENTS.md` 约定由用户自行提交。

**Goal:** 让 `<zh>`、`<yue>`、`<en>`、`<ja>`、`<ko>` 标签在 GPT-SoVITS 的 G2P 阶段保持权威，并在一次 GPT/SoVITS 声学推理中生成一个连续混合音频。

**Architecture:** 应用层继续解析和校验标签，转换为结构化 Segment IR，通过内部 `text_segments` 字段传给引擎。GPT-SoVITS 的 `TextPreprocessor` 对每个片段调用指定语言 G2P、合并 phones/BERT 特征为一个 data item；`TTS.run()` 只对该 data item 执行一次推理。旧的无标签单语言路径保留，`auto` 仅作为明确的兼容诊断模式。

**Tech Stack:** Python 3.10、FastAPI、pytest、GPT-SoVITS v2/v2Pro、PyTorch、Vue 3/Vitest（前端仅保留现有标签输入）。

---

## 当前进度与工作区边界

### 已完成并验证

已有并保留的能力：

- `backend/services/core.py` 已实现五语种标签解析、脚本校验、中文广普转换隔离、英文规范化、显式 Segment IR 和单次混合请求入口。
- `frontend/src/components/LanguageMarkerInput.vue` 已提供五种标签的输入提示；`frontend/src/App.vue` 已移除目标文本的单一语言选择器。
- `backend/tests/test_synthesis_reverse.py`、`backend/tests/test_synthesis_explicit_contract.py` 已覆盖显式请求、语言顺序、请求字段和单次调用契约。
- `tools/gpt_sovits_orb_patch.py` 已为语义 token 回调提供幂等补丁；本计划不把混合语言补丁混入该脚本。
- `tools/gpt_sovits_mixed_language_patch.py` 已提供独立幂等补丁，覆盖 `TextPreprocessor.preprocess_segments()`、`TTS.run()` 的 `segment_mixed` 分支和流式拒绝。
- [设计规范](G:/Clone-youself/docs/MIXED_LANGUAGE_SINGLE_PASS_DESIGN.md) 和 [支持手册](G:/Clone-youself/docs/MIXED_LANGUAGE_SINGLE_PASS_SUPPORT.md) 已写入工作区。

### 当前关键缺口

- 五语种完整有序矩阵、phone/BERT 边界测试和上下文超限测试仍需补齐。
- 生产安装入口尚未自动调用混合语言补丁；这属于后续部署任务，不应在本轮声称已支持。
- `GPT-SoVITS/` 被 `.gitignore` 忽略，不能依靠直接修改该目录交付；必须通过可跟踪的补丁脚本在安装时幂等注入。

基线：`pytest -q backend/tests tests/test_api_core.py tests/test_api_routes.py` 当前为 `80 passed, 3 failed`。失败项是既有 `/api/synthesis` 后台任务和 `/api/batch-synthesis` 兼容测试，与本特性无关；最终验证不得把它们报告为本特性通过。前端基线为 `npm test -- --run` 的 `42 passed`，`npm run build` 成功（只有 Vite 大 chunk 警告）。

本轮应用层验证命令：

```powershell
$env:PYTHONPATH = "."
pytest -q backend/tests/test_synthesis_explicit_contract.py backend/tests/test_synthesis_reverse.py
pytest -q backend/tests/test_gpt_sovits_mixed_language_patch.py
python tools/gpt_sovits_mixed_language_patch.py --help
```

补丁脚本的成功标准是首次运行 `changed=True`，再次运行 `changed=False`；实际 GPT-SoVITS 副本的改动只作为本地验证结果，不作为 Git 交付文件。

## 文件改造总表

| 文件 | 操作 | 职责 |
|---|---|---|
| `backend/services/core.py` | 修改 | Segment IR、显式引擎请求、严格单次路由、日志元数据 |
| `backend/services/synthesis_jobs.py` | 修改 | 透传严格模式状态到任务快照（不改变一次任务一个文件的 API） |
| `backend/tests/test_synthesis_reverse.py` | 修改 | 从 `auto` 期望改为显式请求，增加五语种 325 组组合断言 |
| `backend/tests/test_synthesis_explicit_contract.py` | 新建 | 后端 Segment IR、请求字段、调用次数和错误契约测试 |
| `tools/gpt_sovits_mixed_language_patch.py` | 新建 | 对被忽略的 GPT-SoVITS 工作副本注入显式片段预处理和单次模式 |
| `backend/tests/test_gpt_sovits_mixed_language_patch.py` | 新建 | 补丁幂等、Windows 换行、缺锚点和关键代码注入测试 |
| `setup.ps1` | 后续 | 安装阶段调用混合语言补丁脚本（本轮暂缓） |
| `setup.sh` | 后续 | Linux/macOS 安装阶段调用混合语言补丁脚本（本轮暂缓） |
| `Dockerfile` | 后续 | 镜像构建阶段调用混合语言补丁脚本并验证结果（本轮暂缓） |
| `examples/mixed_language_cases.txt` | 修改 | 扩展五语种正向、反向和边界试听样例 |
| `docs/WEBUI_GUIDE.md` | 修改 | 把现有“自动混合”说明改为严格显式单次模式 |
| `docs/MIXED_LANGUAGE_SINGLE_PASS_DESIGN.md` | 修改 | 补充实际补丁脚本、调用契约和已知限制 |
| `docs/MIXED_LANGUAGE_SINGLE_PASS_SUPPORT.md` | 修改 | 补充部署命令、日志字段和升级排查 |

不需要修改的文件：`frontend/src/components/LanguageMarkerInput.vue`、`frontend/src/App.vue`、`backend/schemas.py`、`backend/app.py`。它们的公开请求仍然只接收原始 `text`；`text_segments` 是后端到引擎的内部字段。

---

### Task 1: 固化显式 Segment IR 和引擎请求契约

**Files:**
- Modify: `backend/services/core.py:30-55,307-370,765-834`
- Create: `backend/tests/test_synthesis_explicit_contract.py`
- Modify: `backend/tests/test_synthesis_reverse.py`

- [ ] **Step 1: Write failing contract tests**

在新测试文件中加入以下断言：

```python
def test_build_engine_segments_keeps_explicit_language_order():
    cfg = {"text_lang": "zh", "auto_lightmid": True}
    segments = core.render_language_segments(
        core.parse_language_segments(
            "<zh>你好</zh><yue>我记得佢</yue><en>AI Voice</en><ja>こんにちは</ja><ko>안녕하세요</ko>"
        ),
        cfg,
    )
    assert [(item["lang"], item["text"]) for item in core.to_engine_segments(segments)] == [
        ("zh", "你好"), ("yue", "我记得佢"), ("en", "AI Voice"),
        ("ja", "こんにちは"), ("ko", "안녕하세요"),
    ]


def test_mixed_request_uses_segment_mixed_and_forwards_segments(monkeypatch):
    captured = []

    def fake_synth_one(cfg, text, semantic_token_cb=None, text_segments=None):
        captured.append((cfg["text_lang"], text, text_segments))
        return [0.0], 24000

    backend = core.SynthesisBackend()
    monkeypatch.setattr(backend, "synth_one", fake_synth_one)
    backend.synth_segments(
        {"text_lang": "zh", "auto_lightmid": False},
        [("zh", "你好"), ("en", "Hello")],
    )
    assert len(captured) == 1
    assert captured[0][0] == "segment_mixed"
    assert [(item["lang"], item["text"]) for item in captured[0][2]] == [
        ("zh", "你好"), ("en", "Hello"),
    ]
```

同步修改 `backend/tests/test_synthesis_reverse.py` 中仍断言 `("auto", ...)` 的测试：它们应断言 `("segment_mixed", ...)` 和 `text_segments`，不再把自动识别结果当作显式标签生效证据。

- [ ] **Step 2: Run the focused tests and verify failure**

运行：

```powershell
pytest -q backend/tests/test_synthesis_explicit_contract.py backend/tests/test_synthesis_reverse.py
```

预期：新契约测试因 `to_engine_segments` 或 `text_segments` 尚不存在而失败；旧测试中与 `auto` 绑定的断言也会失败。失败必须发生在契约层，而不是导入错误。

- [ ] **Step 3: Implement the minimal application contract**

在 `core.py` 增加：

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class Segment:
    text: str
    lang: str
    pause_after_ms: int = 0
    source_span: tuple[int, int] | None = None


def to_engine_segments(segments: list[SynthesisSegment]) -> list[dict[str, Any]]:
    return [
        {"lang": language, "text": text.strip(), "pause_after_ms": 0}
        for language, text in segments
        if text.strip()
    ]
```

将 `SynthesisBackend.synth_one()` 签名扩展为 `text_segments: list[dict[str, Any]] | None = None`。构造 `pipeline.run()` 输入时：

```python
request = {
    "text": text,
    "text_lang": cfg.get("text_lang", "zh"),
    # 其余现有字段保持不变
}
if text_segments is not None:
    request["text_lang"] = "segment_mixed"
    request["text_segments"] = text_segments
```

将 `synth_segments()` 的多语种主路径统一改为：规范化后调用一次 `synth_one(..., text_segments=to_engine_segments(segments))`；不得调用 `compose_mixed_language_text()` 再选择 `auto`。片段级音频拼接代码保留为开发诊断函数，但不再从生产分支进入。

- [ ] **Step 4: Run focused tests and verify pass**

运行同一条 pytest 命令，预期新契约与更新后的反向测试通过；测试中 `synth_one` 调用次数为 1，且每个语言标签按原顺序保留。

---

### Task 2: 增加 GPT-SoVITS 显式片段 G2P 编译器

**Files:**
- Create: `tools/gpt_sovits_mixed_language_patch.py`
- Modify: `backend/tests/test_gpt_sovits_mixed_language_patch.py`
- Modify at install time: `GPT-SoVITS/GPT_SoVITS/TTS_infer_pack/TextPreprocessor.py`（由补丁脚本写入）

- [ ] **Step 1: Write patch-shape tests**

新测试使用临时 GPT-SoVITS 目录，验证补丁脚本：

```python
def test_patch_injects_explicit_segment_preprocessor_idempotently(tmp_path):
    root = make_engine_fixture(tmp_path)
    first = apply_mixed_language_patches(root)
    second = apply_mixed_language_patches(root)
    source = (root / "GPT_SoVITS/TTS_infer_pack/TextPreprocessor.py").read_text(encoding="utf-8")
    assert first.changed is True
    assert second.changed is False
    assert "def preprocess_segments(self, segments, version=\"v2\")" in source
    assert "segment[\"lang\"]" in source
    assert "LangSegmenter.getTexts" not in source.split("def preprocess_segments", 1)[1].split("def get_bert_feature", 1)[0]
```

同时覆盖 UTF-8 BOM/Windows CRLF 保留、缺文件报错和重复运行不重复注入。

- [ ] **Step 2: Run patch tests and verify failure**

运行：

```powershell
pytest -q backend/tests/test_gpt_sovits_mixed_language_patch.py
```

预期：脚本不存在或临时引擎没有显式预处理函数，测试失败。

- [ ] **Step 3: Implement the idempotent patch script**

脚本提供：

```python
@dataclass
class PatchReport:
    root: Path
    changed: bool = False
    messages: list[str] = field(default_factory=list)


def apply_mixed_language_patches(root: str | Path) -> PatchReport:
    """Inject explicit Segment IR compilation without changing model weights."""
```

脚本必须定位 `GPT_SoVITS/TTS_infer_pack/TextPreprocessor.py` 和 `TTS.py`，以唯一锚点替换并在没有锚点时抛出带文件路径的 `RuntimeError`。对 TextPreprocessor 注入的编译逻辑必须等价于：

```python
def preprocess_segments(self, segments, version="v2"):
    phones_list, bert_list, norm_text_list = [], [], []
    boundaries, phone_offset = [], 0
    for index, segment in enumerate(segments):
        lang = segment["lang"]
        text = segment["text"].strip()
        if not text:
            continue
        phones, word2ph, norm_text = self.clean_text_inf(text, lang, version)
        bert = self.get_bert_inf(phones, word2ph, norm_text, lang)
        start = phone_offset
        phones_list.append(phones)
        bert_list.append(bert)
        norm_text_list.append(norm_text)
        phone_offset += len(phones)
        boundaries.append({"index": index, "lang": lang, "start": start, "end": phone_offset})
        if segment.get("pause_after_ms", 0) > 0 and index < len(segments) - 1:
            pause_phones, pause_word2ph, pause_norm = self.clean_text_inf(",", lang, version)
            pause_bert = self.get_bert_inf(pause_phones, pause_word2ph, pause_norm, lang)
            phones_list.append(pause_phones)
            bert_list.append(pause_bert)
            norm_text_list.append(pause_norm)
            phone_offset += len(pause_phones)
    if not phones_list:
        raise ValueError("显式语言片段没有可发音内容。")
    bert = torch.cat(bert_list, dim=1)
    phones = sum(phones_list, [])
    if len(phones) != bert.shape[-1]:
        raise ValueError("显式语言片段的 phones/BERT 长度不一致。")
    return [{"phones": phones, "bert_features": bert, "norm_text": "".join(norm_text_list), "segment_boundaries": boundaries}]
```

实际注入代码应复用现有 `clean_text_inf()`、`get_bert_inf()` 和符号表，不调用 `LangSegmenter`。每段 `lang` 只能来自网关已校验的五种语言；未知语言在入口处拒绝。

- [ ] **Step 4: Run patch tests and inspect generated source**

运行：

```powershell
pytest -q backend/tests/test_gpt_sovits_mixed_language_patch.py
python tools/gpt_sovits_mixed_language_patch.py --help
```

预期：补丁测试全部通过，命令行显示根目录参数和幂等应用结果。

---

### Task 3: 将显式预处理接入 TTS.run 的单次推理路径

**Files:**
- Modify: `tools/gpt_sovits_mixed_language_patch.py`
- Modify at install time: `GPT-SoVITS/GPT_SoVITS/TTS_infer_pack/TTS.py`
- Modify: `backend/tests/test_synthesis_explicit_contract.py`

- [ ] **Step 1: Add failing engine integration tests**

在临时 `TTS.py` fixture 中断言补丁会：

```python
assert '"segment_mixed"' in source
assert 'text_segments = inputs.get("text_segments")' in source
assert 'preprocess_segments(text_segments' in source
```

在后端 fake pipeline 中记录 `run()` 输入，断言一次请求同时包含 `text_lang == "segment_mixed"` 和五个 `text_segments`。

- [ ] **Step 2: Implement the TTS branch**

在引擎配置允许列表中增加内部语言模式 `segment_mixed`，但不加入应用公开语言枚举。`TTS.run()` 读取：

```python
text_segments = inputs.get("text_segments")
```

并在普通非流式预处理分支中使用：

```python
if text_lang == "segment_mixed":
    if not isinstance(text_segments, list) or not text_segments:
        raise ValueError("segment_mixed 需要非空 text_segments。")
    data = self.text_preprocessor.preprocess_segments(text_segments, self.configs.version)
else:
    data = self.text_preprocessor.preprocess(text, text_lang, text_split_method, self.configs.version)
```

严格模式禁止自动 `pre_seg_text()`、`LangSegmenter.getTexts()` 和按句生成多个 data item。流式/分段返回模式第一阶段明确拒绝 `segment_mixed`，避免把“一个音频”误实现成多个声学片段；错误信息必须包含 `segment_mixed` 和“不支持流式”。

在 `TTS_Config.languages` 中仅加入 `segment_mixed` 以通过现有参数断言；不改变现有 `auto`、`auto_yue` 和单语言兼容行为。

- [ ] **Step 3: Add exact call-count and boundary tests**

测试 fake text preprocessor：五个片段分别返回不同 phone 标记，断言最终 `phones` 顺序为 `zh -> yue -> en -> ja -> ko`，且 `preprocess_segments` 只调用一次。另测空片段、未知语言、phone/BERT 长度不一致和上下文超限均抛出定位错误。

- [ ] **Step 4: Run engine-contract tests**

运行：

```powershell
pytest -q backend/tests/test_synthesis_explicit_contract.py backend/tests/test_gpt_sovits_mixed_language_patch.py
```

预期：后端只向 pipeline 发起一次 `run()`；引擎分支只产生一个 data item；所有边界测试通过。

---

### Task 4: 把严格模式接入任务进度、日志和兼容回滚

**Files:**
- Modify: `backend/services/core.py`
- Modify: `backend/services/synthesis_jobs.py`
- Modify: `backend/tests/test_synthesis_reverse.py`
- Modify: `tests/test_api_core.py`

- [ ] **Step 1: Add progress metadata assertions**

扩展现有进度快照断言，要求混合行包含：

```python
{
    "synthesis_mode": "single_pass_explicit",
    "segment_count": 5,
    "acoustic_inference_calls": 1,
}
```

单语言无标签行保持 `legacy_single` 或现有兼容字段，不影响前端已有进度显示。

- [ ] **Step 2: Implement metadata propagation**

`core.synthesize_lines()` 在每行解析后计算 `segment_count`，将 `synthesis_mode="single_pass_explicit"` 写入 generating、output_completed 和最终结果快照。`SynthesisBackend.synth_one()` 在一次 pipeline 调用前后维护请求级计数；异常时也保留已发生的调用数。

`SynthesisJobManager` 只复制和转发这些字段，不重新解释模式。`compat_auto` 只能通过开发配置显式启用，生产默认不启用；严格模式失败时不自动重试为 `auto` 或片段音频拼接。

- [ ] **Step 3: Verify existing API behavior**

运行：

```powershell
pytest -q tests/test_api_core.py tests/test_synthesis_jobs.py backend/tests/test_synthesis_reverse.py
```

预期：进度快照仍有 `status/phase/current/total/outputs`，并新增模式字段；现有后台任务流不改变。

---

### Task 5（暂缓）: 将引擎补丁纳入 Windows、Linux/macOS 和 Docker 安装

> 本轮不执行本任务。后续恢复时，必须先验证固定 GPT-SoVITS commit 的锚点，再同时更新三个安装入口和对应测试；在此之前不能把安装后自动具备严格混合模式写入用户文档。

**Files:**
- Modify: `setup.ps1`
- Modify: `setup.sh`
- Modify: `Dockerfile`
- Modify: `backend/tests/test_gpt_sovits_mixed_language_patch.py`
- Modify: `tests/test_docker_config.py`

- [ ] **Step 1: Add installer contract tests**

断言三个安装入口都调用同一个脚本：

```python
assert "gpt_sovits_mixed_language_patch.py" in powershell
assert "gpt_sovits_mixed_language_patch.py" in shell
assert "gpt_sovits_mixed_language_patch.py" in dockerfile
```

并断言 Docker 构建在脚本后检查 `preprocess_segments` 和 `segment_mixed`。

- [ ] **Step 2: Add idempotent installer calls**

在现有 `gpt_sovits_orb_patch.py` 调用之后、删除临时脚本之前调用：

```powershell
& $PythonExe (Join-Path $RepoRoot "tools\gpt_sovits_mixed_language_patch.py") $GPT_SOVITS_ROOT
```

```bash
"$PYBIN" "$REPO_ROOT/tools/gpt_sovits_mixed_language_patch.py" "$GPT_ROOT"
```

Docker 使用同一 Python：

```dockerfile
COPY tools/gpt_sovits_mixed_language_patch.py /tmp/gpt_sovits_mixed_language_patch.py
RUN python /tmp/gpt_sovits_mixed_language_patch.py /workspace/GPT-SoVITS \
    && grep -q preprocess_segments GPT_SoVITS/TTS_infer_pack/TextPreprocessor.py \
    && grep -q segment_mixed GPT_SoVITS/TTS_infer_pack/TTS.py \
    && rm /tmp/gpt_sovits_mixed_language_patch.py
```

补丁脚本必须使用当前引擎 commit 的锚点；锚点变化时构建失败并给出文件路径，不得生成一个未打补丁但看似可用的镜像。

- [ ] **Step 3: Run installer/config tests**

运行：

```powershell
pytest -q backend/tests/test_gpt_sovits_mixed_language_patch.py tests/test_docker_config.py
```

预期：脚本路径、解释器选择、Docker 检查项和幂等性全部通过。

---

### Task 6: 完善五语种反向测试和可试听样例

**Files:**
- Modify: `backend/tests/test_synthesis_reverse.py`
- Create: `backend/tests/test_synthesis_five_language_matrix.py`
- Modify: `examples/mixed_language_cases.txt`

- [ ] **Step 1: Add the complete ordered matrix**

使用 `itertools.permutations` 和 `combinations` 生成五种语言全部有序组合，共 325 组；每组调用 fake backend，断言：

```python
assert pipeline_calls == 1
assert request["text_lang"] == "segment_mixed"
assert [part["lang"] for part in request["text_segments"]] == list(order)
```

增加重复语言、相邻语言、标点边界、纯数字英语、`AI Voice`、`Wi-Fi`、短片段和长片段测试。测试中禁止 monkeypatch 后的显式路径调用 `LangSegmenter`。

- [ ] **Step 2: Add negative matrix**

覆盖以下输入并断言在声学调用前失败：

```text
<en>你好</en>
<ja>hello</ja>
<ko>hello</ko>
<yue></yue>
<en>Hello
<fr>bonjour</fr>
```

错误消息必须指出标签或片段位置，不得输出静音音频。

- [ ] **Step 3: Expand the listening file**

将 `examples/mixed_language_cases.txt` 扩展为：五语种正向顺序、五语种反向顺序、每种语言作为首段/末段、连续 `AI Voice`、粤语汉字和中日韩夹英文。每行保持一条合成任务，注释说明预期听感和重点问题。

- [ ] **Step 4: Run matrix tests**

运行：

```powershell
pytest -q backend/tests/test_synthesis_five_language_matrix.py backend/tests/test_synthesis_reverse.py
```

预期：325 组组合均保持一次 pipeline 调用，顺序和语言字段无漂移。

---

### Task 7: 更新用户文档并执行全量验证

**Files:**
- Modify: `docs/WEBUI_GUIDE.md`
- Modify: `docs/MIXED_LANGUAGE_SINGLE_PASS_DESIGN.md`
- Modify: `docs/MIXED_LANGUAGE_SINGLE_PASS_SUPPORT.md`
- Modify: `README.md`

- [ ] **Step 1: Replace misleading automatic-mix wording**

把现有“优先使用 GPT-SoVITS 原生模式/自动混合识别”的描述改为：标签在 G2P 阶段强制语言，声学模型一次推理；`auto` 只用于显式兼容诊断。补充安装脚本会对本地 GPT-SoVITS 应用混合语言补丁，并说明补丁对应的固定 commit。

- [ ] **Step 2: Document operational checks**

文档必须给出以下日志判定：

```text
synthesis_mode=single_pass_explicit
segment_count=5
acoustic_inference_calls=1
```

并明确参考音频仍只有一个音色提示，不会自动补齐缺失语种的母语韵律。

- [ ] **Step 3: Run documentation consistency checks**

运行：

```powershell
rg -n "auto_yue|自动混合识别|segment_mixed|single_pass_explicit|acoustic_inference_calls" docs README.md
git diff --check
```

预期：生产说明不再把 `auto` 描述为显式标签主路径；设计稿、支持手册和 WebUI 指南使用相同字段名和模式定义。

- [ ] **Step 4: Run final verification**

运行：

```powershell
pytest -q
Push-Location frontend
npm test -- --run
npm run build
Pop-Location
```

预期：新增混合语言测试、现有后端测试和前端测试通过。若仍有基线 API 路由失败，必须在最终报告中列出具体测试名和失败原因，不能宣称全量通过。

## 实现完成判定

只有同时满足以下条件才算完成：

1. 五种语言标签均能进入对应 G2P，主路径不调用自动语言识别。
2. 任意五语种有序组合的 pipeline 声学调用计数为 1。
3. `phones`、BERT 特征和语言边界顺序一致，非法输入在推理前失败。
4. Windows、Linux/macOS 和 Docker 安装都会应用同一混合语言补丁。
5. 生产日志明确显示 `single_pass_explicit`，兼容模式不会静默触发。
6. 文档、试听样例和反向测试覆盖五语种，而不只覆盖中文/粤语。

按 `AGENTS.md` 约定，本计划执行过程中不代用户执行 `git commit`、`git push`、`git reset` 或 `git checkout --`。
