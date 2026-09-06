import { describe, expect, it } from 'vitest'
import { createDraftsStore } from './drafts'
import draftsSource from './drafts.ts?raw'

function memoryStorage() {
  const map = new Map<string, string>()
  return {
    getItem: (key: string) => map.get(key) ?? null,
    setItem: (key: string, value: string) => { map.set(key, value) },
    removeItem: (key: string) => { map.delete(key) },
  }
}

describe('createDraftsStore', () => {
  it('works without receiving an explicit storage argument', () => {
    const store = createDraftsStore()
    const drafts = store.add('默认存储可用')
    expect(drafts).toHaveLength(1)
    expect(drafts[0].text).toBe('默认存储可用')
  })

  it('resolves the default storage defensively instead of touching localStorage eagerly', () => {
    expect(draftsSource).toContain('storage?: MinimalStorage')
    expect(draftsSource).toContain('typeof globalThis.localStorage')
    expect(draftsSource).toContain('memoryFallbackStorage()')
  })

  it('drops malformed persisted drafts', () => {
    const raw = JSON.stringify([
      { id: '  ', text: '空白 id', savedAt: 1 },
      { id: 'a', text: '非有限时间', savedAt: Number.NaN },
      { id: 'b', text: '有效草稿', savedAt: 3 },
    ])
    const store = createDraftsStore({
      getItem: () => raw,
      setItem: () => {},
      removeItem: () => {},
    })
    expect(store.load().map((draft) => draft.id)).toEqual(['b'])
  })

  it('adds a draft and dedupes by trimmed text, newest first', () => {
    const store = createDraftsStore(memoryStorage())
    store.add('  各位听众朋友  ')
    const drafts = store.add('各位听众朋友')
    expect(drafts).toHaveLength(1)
    expect(drafts[0].text).toBe('各位听众朋友')
  })

  it('keeps at most 12 drafts and drops the oldest', () => {
    const store = createDraftsStore(memoryStorage())
    for (let index = 0; index < 14; index += 1) store.add(`台词 ${index}`)
    const drafts = store.load()
    expect(drafts).toHaveLength(12)
    expect(drafts[0].text).toBe('台词 13')
  })

  it('removes a draft by id and ignores blank input', () => {
    const store = createDraftsStore(memoryStorage())
    const [first] = store.add('第一句')
    expect(store.remove(first.id)).toHaveLength(0)
    expect(store.add('   ')).toHaveLength(0)
  })
})
