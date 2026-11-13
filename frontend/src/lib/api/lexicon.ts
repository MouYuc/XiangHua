import type { LexiconEntry } from './types'
import { api, request } from './client'

export const listLexicon = () => api.get<{ entries: LexiconEntry[] }>('/api/lexicon')

export const upsertLexicon = (body: { term: string; replacement: string; note?: string; enabled?: boolean }) =>
  api.post<{ message: string; entry: LexiconEntry }>('/api/lexicon', body)

export const deleteLexicon = (term: string) =>
  request<void>(`/api/lexicon?term=${encodeURIComponent(term)}`, { method: 'DELETE' })
