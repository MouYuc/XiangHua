import type { ReferenceAudioPrepareSnapshot, ReferenceAudioPrepareStart } from './types'
import { UPLOAD_TIMEOUT_MS, request } from './client'

/** 上传一次，后端在后台完成切片 / 质检 / 转写。 */
export const startReferenceAudioPrepare = (body: FormData) =>
  request<ReferenceAudioPrepareStart>('/api/reference-audio/prepare', {
    method: 'POST',
    body,
    // Uploading a long recording is streamed to disk before the job starts.
    timeoutMs: UPLOAD_TIMEOUT_MS,
  })

export const getReferenceAudioPrepareSnapshot = (jobId: string) =>
  request<ReferenceAudioPrepareSnapshot>(`/api/reference-audio/prepare/${encodeURIComponent(jobId)}`)
