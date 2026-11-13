import type { ProvenanceResult } from './types'
import { api } from './client'

export const verifyProvenance = (body: { path?: string; download_id?: string }) =>
  api.post<ProvenanceResult>('/api/provenance/verify', body)
