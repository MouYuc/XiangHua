import type { HistoryEntry } from './types'
import { api } from './client'

export const fetchHistory = () => api.get<HistoryEntry[]>('/api/history')

export const clearHistory = () => api.delete<void>('/api/history')
