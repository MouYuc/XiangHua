import type { EpisodeExportResult } from './types'
import { api } from './client'

// —— 整集交付导出：批量合成（SRT + 整集音轨）与多角色对话共用同一结果结构 ——

export const exportBatch = (jobId: string) =>
  api.post<EpisodeExportResult>('/api/batch/export', { job_id: jobId })

export const exportDialogue = (jobId: string) =>
  api.post<EpisodeExportResult>('/api/dialogue/export', { job_id: jobId })
