# /v1 API 契约

> 状态：已挂载，与内部 `/api` 共用同一套 handler
> 更新：2026-10-25（新增访问控制与限流）
> 说明：`/v1` 是对外稳定面，`/api` 是 WebUI 内部接口，两者行为一致但只有 `/v1` 承诺兼容性

---

## 0. 访问控制（2026-10-25 起）

所有 `/api` 与 `/v1` 请求先经过 `RequestGuardMiddleware`（`backend/services/request_guard.py`）：

| 策略 | 规则 | 关闭/调整方式 |
|---|---|---|
| Host 校验 | `Host` 必须是 `localhost / 127.0.0.1 / [::1]`（防 DNS rebinding） | `XIANGHUA_ALLOWED_HOSTS=a,b` 自定义；`XIANGHUA_ALLOW_REMOTE_HOSTS=1` 关闭（局域网部署时用） |
| Origin 校验 | 带 `Origin` 的请求只允许本机来源（防 CSRF）；curl/同源 GET 不带 Origin 不受影响 | 无独立开关 |
| /v1 API Key | 设置 `XIANGHUA_API_KEY=<key>` 后，`/v1` 必须带 `Authorization: Bearer <key>` 或 `X-API-Key: <key>`，否则 401；设置 `XIANGHUA_API_KEYS_FILE=<path>` 进入多租户模式（`/api/api-keys` 管理租户 Key、日配额与 RPM，配额超限返回 429，合成按字符计量） | 两者都不设置即开放（仅限本机回环使用） |
| 合成队列 | `POST /synthesis` 在排队+运行任务 ≥ 8 时返回 429 `RATE_LIMITED` | `XIANGHUA_SYNTHESIS_QUEUE_MAX=N` |
| 合成体积 | `POST /synthesis` 请求体 > 1 MiB 返回 413 | `XIANGHUA_SYNTHESIS_MAX_BODY_BYTES=N` |

路径类入参另有圈禁：`/reference-audio/inspect`、`/timbre/convert-file` 只接受仓库目录内的路径（`XIANGHUA_ALLOWED_PATH_ROOTS` 可追加白名单根）；`/environment/select` 只接受扫描发现的候选环境；发音补丁导入默认关闭（`XIANGHUA_ALLOW_PRONUNCIATION_PATCH=1` 开启，且补丁只能改 `GPT_SoVITS/text/` 下文件）；媒体上传默认上限 2 GiB（`XIANGHUA_MAX_MEDIA_BYTES`）。测试见 `backend/tests/test_security_guards.py`。

---

## 1. 为什么有两个前缀

```text
前端 WebUI  → /api/*   （内部，随 UI 演进，不承诺稳定）
SDK / 客户   → /v1/*    （对外，只增不改）
```

两个前缀由同一个 `APIRouter` 同时挂载，因此**不存在"对外版和内部版逻辑不一致"**的可能。测试 `test_api_versioning.py` 会校验：任何 `/api/x` 都必须存在同方法、同响应的 `/v1/x`。

```python
app.include_router(router, prefix="/api")
app.include_router(router, prefix="/v1")
```

未知路径一律返回 JSON 404，不会回落到 WebUI 的 `index.html`。

- OpenAPI 文档：`GET /openapi.json`、`GET /docs`
- 当前规模：67 条路径 × 2 个前缀（2026-10-25 新增：`/lexicon`、`/batch/export`、`/dialogue/*`、`/provenance/verify`、`/api-keys`）

---

## 2. 版本与兼容策略

| 规则 | 说明 |
|---|---|
| 路径版本 | `/v1` 内不做破坏性变更；必须破坏时新开 `/v2`，`/v1` 保留过渡期 |
| 响应字段 | 只增不改不删；客户端必须忽略未知字段 |
| 错误模型 | `detail` 保持人类可读中文；`code` 是稳定机器码，新增 code 不视为破坏 |
| 内部接口 | `/api` 可随 WebUI 调整，不对外承诺 |

---

## 3. 认证

`/v1` 现在支持两种模式（见 §0）：不设置任何 Key 时仅限本机回环使用；设置 `XIANGHUA_API_KEY` 或 `XIANGHUA_API_KEYS_FILE` 后，每个 `/v1` 请求必须携带：

```http
Authorization: Bearer <api_key>
```

多租户模式下按 Key 计量字符数、限制日请求配额与每分钟速率；Key 只存 SHA-256 哈希。按 scope 细粒度限权（`voices:read` / `jobs:write` / `licenses:read` …）尚未实现，规划见 `docs/B2B_VOICE_API_STRATEGY.md`。

---

## 4. 错误模型

所有错误响应：

```json
{
  "detail": "GPU 正被「训练 s2」占用，合成等待了 60 秒仍未轮到。",
  "code": "GPU_BUSY"
}
```

| HTTP | code | 典型场景 |
|---:|---|---|
| 400 | `BAD_REQUEST` | 参数不合法 |
| 400 | `MEDIA_INGEST_FAILED` | 媒体上传/分析失败（消息可直接展示给用户） |
| 404 | `NOT_FOUND` | 会话/任务/数据集不存在 |
| 409 | `GPU_BUSY` | 另一个任务占用 GPU 超时 |
| 409 | `CONFLICT` | 任务不可取消等状态冲突 |
| 422 | —— | 请求体校验失败，保持 FastAPI 默认结构（`detail` 为数组） |
| 500 | `INTERNAL_ERROR` | 未预期异常 |

WebUI 只读 `detail`，所以加 `code` 不影响旧前端。

---

## 5. 异步约定

长任务统一"提交 → 拿 job_id → 轮询或订阅"：

| 方式 | 接口 |
|---|---|
| 轮询 | `GET /v1/synthesis/status/{job_id}`、`GET /v1/jobs/{job_id}` |
| SSE | `GET /v1/synthesis/events/{job_id}` |
| 统一视图 | `GET /v1/jobs`（聚合合成/建档/音色/训练/媒体五类任务） |
| 取消 | `POST /v1/jobs/{job_id}/cancel`（目前仅训练支持，其余 409） |

任务状态统一为 `queued / running / done / failed / cancelled / unknown`。

---

## 6. 端点清单

> 以 `/openapi.json` 为准；下表按能力分组。

### 6.1 基础

| 方法 | 路径 |
|---|---|
| GET | `/v1/health` |
| GET | `/v1/config` |
| PUT | `/v1/config` |
| GET | `/v1/overview` |
| GET | `/v1/voice/options` |
| GET | `/v1/environment` |
| POST | `/v1/environment/scan` |
| POST | `/v1/environment/select` |
| POST | `/v1/doctor` |

`/v1/overview` 额外返回 `gpu: {busy, holder, queue, vram, history}`。

### 6.2 任务

| 方法 | 路径 |
|---|---|
| GET | `/v1/jobs` |
| GET | `/v1/jobs/{job_id}` |
| POST | `/v1/jobs/{job_id}/cancel` |

### 6.3 音色样本（一段自然说话音频自动建档）

| 方法 | 路径 |
|---|---|
| POST | `/v1/reference-audio` （同步，兼容旧调用） |
| POST | `/v1/reference-audio/prepare` （异步，返回 job_id） |
| GET | `/v1/reference-audio/prepare/{job_id}` |
| POST | `/v1/reference-audio/inspect` |
| GET | `/v1/reference-audio/prompt?path=...` |

### 6.3a 声线卡（版本化 VoiceCard / StylePack 契约）

| 方法 | 路径 |
|---|---|
| GET | `/v1/voice-cards` |
| POST | `/v1/voice-cards` （把当前配置的参考音/底模快照为新卡 v`date.1`） |
| GET | `/v1/voice-cards/{voice_id}` |
| PATCH | `/v1/voice-cards/{voice_id}` （display_name / status=active|retired / license_id / consent_id / active_version） |
| POST | `/v1/voice-cards/{voice_id}/versions` （追加不可变版本并设为当前） |
| POST | `/v1/voice-cards/{voice_id}/activate` （把当前版本写回合成配置） |

行为要点：

- 版本**只增不改**：新增版本追加 `date.N`，历史版本永不改写；`PATCH active_version` 可回滚到任一历史版本；
- `POST /v1/synthesis` 接受可选 `voice_card_id` 与 `style_id`；优先级为**请求显式字段 > 卡内默认 > 保存的配置**；
- `status=retired` 的卡拒绝新合成（撤销语义），也不能 `activate`；
- `timbre_available=false`（参考音文件缺失）只提示不拦截，与质量门禁同一哲学；
- 风格包资产：`assets/style_packs.json`（natural / calm / energetic / narrative），映射到 `speed`、`pause_second`；
- `GET /v1/voice/options` 响应新增 `styles` 数组（只增字段，不破坏旧客户端）。

### 6.4 合成

| 方法 | 路径 |
|---|---|
| POST | `/v1/synthesis` |
| GET | `/v1/synthesis/status/{job_id}` |
| GET | `/v1/synthesis/events/{job_id}` |
| GET | `/v1/synthesis/readiness` |
| POST | `/v1/synthesis/readiness/retry` |
| POST | `/v1/engine/pronunciation-patch` |

### 6.5 音色转换

| 方法 | 路径 |
|---|---|
| GET | `/v1/timbre/profile` |
| POST | `/v1/timbre/profile/build` |
| GET | `/v1/timbre/sources` |
| POST | `/v1/timbre/convert` |
| POST | `/v1/timbre/convert-file` |
| GET | `/v1/timbre/status/{job_id}` |
| GET | `/v1/timbre/jobs` |
| POST | `/v1/timbre/unload` |

### 6.6 文件与历史

| 方法 | 路径 |
|---|---|
| GET | `/v1/files` |
| GET | `/v1/files/{download_id}` |
| GET | `/v1/history` |
| DELETE | `/v1/history` |

> 注意：`/v1/files/{download_id}` 没有鉴权也没有过期时间。对外开放前必须换成短期签名 URL。

### 6.7 训练

| 方法 | 路径 |
|---|---|
| GET | `/v1/training/status` |
| GET | `/v1/training/metrics` |
| GET | `/v1/training/checkpoints` |
| POST | `/v1/training/dependencies` |
| POST | `/v1/training/patch` |
| POST | `/v1/training/slice` |
| POST | `/v1/training/asr` |
| GET | `/v1/training/transcript` |
| PUT | `/v1/training/transcript` |
| POST | `/v1/training/transcript/skip` |
| POST | `/v1/training/features` |
| POST | `/v1/training/run` |
| POST | `/v1/training/stop` |
| GET | `/v1/training/log` |
| POST | `/v1/training/models/select` |

### 6.8 媒体采集

| 方法 | 路径 |
|---|---|
| POST | `/v1/media/sessions` |
| GET | `/v1/media/sessions` |
| GET | `/v1/media/sessions/{session_id}` |
| POST | `/v1/media/sessions/{session_id}/analyze` |
| GET | `/v1/media/sessions/{session_id}/job` |
| POST | `/v1/media/sessions/{session_id}/select-speaker` |
| POST | `/v1/media/sessions/{session_id}/export` |
| POST | `/v1/media/sessions/{session_id}/activate` |
| GET | `/v1/media/sessions/{session_id}/dataset/{dataset_id}/{filename}` |

`export` / `activate` 支持 `dataset_id` 与 `run_id`，可重放历史分析版本；分析结果按 `segments/<run_id>/` 只增不删。

---

## 7. 尚未提供（对外开放前必须补）

1. API Key / 租户 / 配额 / 限流；
2. 签名下载 URL（替代 `/v1/files/{id}`）；
3. `Idempotency-Key`（批量提交防重复）；
4. 统一错误码文档的机器可读版本（OpenAPI `components`）；
5. `/v1` 专属的请求示例集合（Postman / Python SDK）。
