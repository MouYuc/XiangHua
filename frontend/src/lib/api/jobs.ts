import type { JobsResponse } from './types'
import { api } from './client'

export const fetchJobs = (limit = 50) => api.get<JobsResponse>(`/api/jobs?limit=${limit}`)

export const cancelJob = (jobId: string) =>
  api.post<{ message: string }>(`/api/jobs/${encodeURIComponent(jobId)}/cancel`)
