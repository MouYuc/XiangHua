export type Draft = { id: string; text: string; savedAt: number }

const STORAGE_KEY = 'xianghua.drafts'
const MAX_DRAFTS = 12

type MinimalStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

function makeId() {
  return `d-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
}

function memoryFallbackStorage(): MinimalStorage {
  const map = new Map<string, string>()
  return {
    getItem: (key) => map.get(key) ?? null,
    setItem: (key, value) => { map.set(key, value) },
    removeItem: (key) => { map.delete(key) },
  }
}

function resolveDefaultStorage(): MinimalStorage {
  try {
    if (typeof globalThis.localStorage === 'undefined') return memoryFallbackStorage()
    return globalThis.localStorage
  } catch {
    return memoryFallbackStorage()
  }
}

export function createDraftsStore(storage?: MinimalStorage) {
  const target = storage ?? resolveDefaultStorage()
  function load(): Draft[] {
    try {
      const raw = target.getItem(STORAGE_KEY)
      if (!raw) return []
      const parsed: unknown = JSON.parse(raw)
      if (!Array.isArray(parsed)) return []
      return parsed.filter((item): item is Draft => {
        const draft = item as Draft
        return (
          typeof draft.id === 'string' && draft.id.trim() !== ''
          && typeof draft.text === 'string'
          && Number.isFinite(draft.savedAt)
        )
      })
    } catch {
      return []
    }
  }

  function save(drafts: Draft[]) {
    try {
      if (!drafts.length) target.removeItem(STORAGE_KEY)
      else target.setItem(STORAGE_KEY, JSON.stringify(drafts))
    } catch {
      /* 存储不可用时静默降级，草稿只留在内存 */
    }
  }

  function add(text: string): Draft[] {
    const trimmed = text.trim()
    if (!trimmed) return load()
    const next = [
      { id: makeId(), text: trimmed, savedAt: Date.now() },
      ...load().filter((item) => item.text !== trimmed),
    ].slice(0, MAX_DRAFTS)
    save(next)
    return next
  }

  function remove(id: string): Draft[] {
    const next = load().filter((item) => item.id !== id)
    save(next)
    return next
  }

  function clear() {
    save([])
  }

  return { load, add, remove, clear }
}
