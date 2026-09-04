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

## 工作边界

- Docker 已放弃，不恢复 Docker。
- 不替用户执行 `git commit`、`git push`、`git reset` 或 `git checkout --`。
- 保留工作区已有改动。
- 默认 Python：`G:\Clone-youself\.venv\Scripts\python.exe`。
- 模型运行前设置 `PYTHONIOENCODING=utf-8`。
