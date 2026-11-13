import type { DialogueSnapshot } from './types'
import { api } from './client'

export const startDialogue = (script: string, roles: Record<string, string>) =>
  api.post<DialogueSnapshot>('/api/dialogue/synthesis', { script, roles })

export const dialogueStatus = (jobId: string) =>
  api.get<DialogueSnapshot>(`/api/dialogue/status/${encodeURIComponent(jobId)}`)
