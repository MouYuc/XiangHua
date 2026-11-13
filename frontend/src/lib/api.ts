/**
 * API 客户端入口（兼容保留）。
 *
 * 实际内容按域拆在同目录模块：类型契约在 `./api/types`，请求核心在
 * `./api/client`，端点函数在 reference / voiceCards / jobs / history /
 * lexicon / dialogue / export / provenance。全仓 `from '.../lib/api'`
 * 的 import 路径经这里 re-export 保持不变。
 */
export * from './api/types'
export * from './api/client'
export * from './api/reference'
export * from './api/voiceCards'
export * from './api/jobs'
export * from './api/history'
export * from './api/lexicon'
export * from './api/dialogue'
export * from './api/export'
export * from './api/provenance'
