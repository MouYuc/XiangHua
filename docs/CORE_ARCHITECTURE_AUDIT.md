# 核心架构自检与收敛记录

> 日期：2026-10-17
> 范围：GPU 使用、媒体数据集、后台任务、数据安全
> 结论：单人串行本来就安全；本轮把"同时开两件事"和"重跑分析"这两个会咬人的口子补上了

---

## 0. 为什么会有这份文档

B2B 平台化之前，先回答一个问题：**当前这套东西的"工作架构"本身稳不稳？**

判断方式不是读代码感觉，而是问 8 个具体问题，看能不能给出确定答案。下面是对照表。

---

## 1. 自检 8 问：改之前 vs 改之后

| # | 问题 | 改之前 | 改之后 |
|---|---|---|---|
| 1 | 合成和训练同时开安全吗？ | ❌ 无人调度，抢显存，后到的 OOM | ✅ 合成/训练/媒体分析/音色转换共用进程级租约，FIFO 排队 |
| 2 | 两个不同音色的合成同时跑？ | ❌ 全局后端会 `init_t2s_weights` 互换权重 | ✅ 租约在 `ensure()` 外侧且覆盖整次合成，任务不会互相换权重 |
| 3 | 合成中关掉浏览器会怎样？ | ✅ daemon 线程继续跑 | ✅ 不变 |
| 4 | 后端重启后，正在跑的任务呢？ | ⚠️ 全丢；训练状态可能残留 running | ⚠️ 仍然全丢（有意保留，见第 5 节），但现在 `GET /api/jobs` 能一眼看清哪些在跑 |
| 5 | 重跑一次媒体分析，上次的切片还在吗？ | ❌ `shutil.rmtree(segments)` 直接删 | ✅ 每次分析写 `segments/<run_id>/`，只增不删 |
| 6 | 切换 GPT-SoVITS 环境要重启吗？ | ❌ 是 | ❌ 仍是（模型常驻，属已知限制） |
| 7 | 训练集被覆盖能回到上一版吗？ | ⚠️ 只有 `.bak`，要手工救 | ✅ 可以按 `dataset_id` 重新激活任一历史数据集 |
| 8 | 显存不够时谁被牺牲？ | ❌ 无人调度，随机 OOM | ✅ 由租约决定顺序；排队方等待 60 秒后收到带持有者名字的中文提示 |

**8 问里 6 个变绿，1 个仍红（#6 环境切换），1 个有意保留（#4 任务持久化）。**

---

## 2. 四个结构性原因与处理

### F1 · 五套任务状态机

合成、单音频建档、音色转换、训练、媒体分析各有一套进度/失败/取消语义。

**处理**：新增 `backend/services/job_registry.py`，只读聚合五套状态，统一成
`job_id / kind / title / status / progress / stage / error / started_at / updated_at / cancelable / source`。
对外暴露：

```text
GET  /api/jobs?limit=50      → {jobs, counts, by_kind, errors}
GET  /api/jobs/{job_id}      → {job} 或 404
POST /api/jobs/{job_id}/cancel → 仅训练可取消，其余 409
```

**没有重写执行体**：合成/建档/音色/训练/媒体各自的管理器不变，注册表只是门面。任一来源抛异常都会进 `errors`，不会让 `/api/jobs` 500。

### F2 · 训练集与切片是全局单例，且重跑是破坏性的

**处理**：媒体采集改为 append-only 版本目录：

```text
custom_data/media/<session_id>/segments/<run_id>/
  seg_0001.wav …
  segments.json          # 该次分析的冻结片段列表（含转写）
```

- `session.json` 新增 `runs` 与 `active_run`；`segments` 仍等于 active run，前端/旧接口无感；
- `export_dataset(..., run_id=)` 可导出被取代的旧版本；
- `activate_dataset(..., dataset_id=)` 按数据集 manifest 原样重放，**不再受之后重跑影响**；
- 旧会话（切片在 `segments/seg_*.wav`）隐式视为 `run_legacy`，不迁移、不改写磁盘。

### F3 · 全局模型单例 + 配置热切换的并发语义未定义

**处理**：新增 `backend/services/gpu_arbiter.py`，进程级租约：

```python
with GPU_ARBITER.lease("synthesis", job_id="", label="合成 12 句"):
    ...
```

接入点：

| 调用点 | kind | 超时 |
|---|---|---|
| 合成（`core.synthesize_lines`，含 `ensure`） | `synthesis` | 默认 60s |
| 模型加载/换权重（`SynthesisBackend.ensure`） | `model_load` | 默认 60s，可重入 |
| 预热线程 | 同上 | 忙时状态回到 `idle`，不报配置错误 |
| 媒体分析（`MediaIngestService.analyze`） | `media_analysis` | 无（长任务） |
| 单音频建档（`voice_ingest`） | `voice_prepare` | 默认 60s |
| 音色转换 / 匹配池 | `timbre` | 无 |
| 训练（`tools.train.start_job`） | `training` | 无 |

`/api/overview` 现在带 `gpu: {busy, holder, queue, vram, history}`；忙满 60 秒抛 `GpuBusyTimeout`（中文，含持有者），HTTP 层映射为 **409 `GPU_BUSY`**。

顺序约定：**先租约，后服务锁**。仲裁器同线程可重入，避免嵌套死锁。

### F4 · 进程级 `os.chdir`

**未改**，但收紧了边界：`ensure()` 现在一定在租约内执行，`synth_one` 的 `chdir` 期间不会有第二个任务插进来换状态。其余服务仍依赖"全部使用绝对路径"这一纪律；新增代码必须遵守。

---

## 3. 新增的不变量（都写成了测试）

| 不变量 | 测试 |
|---|---|
| 第二个 GPU 任务必须排队，不能与第一个交叉 | `test_core_architecture_contracts.test_second_synthesis_waits_instead_of_interleaving` |
| 合成全程持有租约（含 `ensure`） | `test_synthesis_holds_the_lease_across_ensure_and_inference` |
| 模型加载单独持租约 | `test_model_loading_takes_its_own_lease` |
| 单行失败后租约必须释放 | `test_lease_is_released_when_a_line_fails` |
| FIFO、超时、可重入、状态快照 | `test_gpu_arbiter`（7 条） |
| 两次分析并存、旧切片不删 | `test_media_ingest`（双 run 用例） |
| 按 `dataset_id` 重放旧数据集 | `test_media_ingest`（重放用例） |
| 旧会话兼容 | `test_media_ingest`（legacy 用例） |
| 五源聚合、容错、取消语义 | `test_job_registry`（21 条） |
| `/api/jobs` 契约与 404/409 | `test_jobs_api`（4 条） |

---

## 4. 验证快照

- 后端 unittest：**303 项全过**
- pytest（`test_synthesis_reverse.py`，unittest 不收集）：**31 项全过**
- 前端：**125 项全过**；`npm run build` 通过
- `compileall`、`git diff --check`：通过
- 真实链路：真实录音异步建档 + 真实合成后，`/api/jobs` 能看到 synthesis 任务、`/api/overview.gpu` 在合成期间显示持有者，完成后回到空闲
- API 双挂载：`/api` 与 `/v1` 各 55 条路径，方法逐一对应；未知路径返回 JSON 404（`{"detail","code"}`）

---

## 5. 有意保留的缺口

1. **任务状态不持久化**。五套 job 仍随进程消失；`voice_prepare_jobs` 里已标注替换点（只换存储层）。
2. **环境切换需重启**。`ensure()` 只允许一个 `gpt_sovits_root`。
3. **训练子进程与 CLI 脚本不经过仲裁器**。用 `scripts/*.py` 直接跑训练时，WebUI 不知道 GPU 被占。
4. **前端未接 `/api/jobs` 与 `gpu` 状态**。接口已就绪，可视化留到下一步。
5. **run/dataset 只增不删**，目前没有清理策略，长期占盘。
6. **前端是 1900 行单文件**（5 个页面），属于 UI 层技术债，不影响本次架构结论。
