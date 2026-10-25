import { beforeEach, describe, expect, it, vi } from 'vitest'

describe('usePage', () => {
  beforeEach(() => {
    vi.resetModules()
    window.location.hash = ''
  })

  it('defaults to workbench and navigates through the hash', async () => {
    const { usePage } = await import('./usePage')
    const { page, setPage, meta } = usePage()
    expect(page.value).toBe('workbench')
    expect(meta.value.crumb).toBe('工作台')

    setPage('voices')
    expect(page.value).toBe('voices')
    expect(window.location.hash).toBe('#/voices')
    expect(meta.value.crumb).toBe('声音库')
  })

  it('follows browser hash changes, falls back on unknown pages, and marks voice sub-flows', async () => {
    const { usePage } = await import('./usePage')
    const { page, isVoiceSubFlow } = usePage()

    window.location.hash = '#/history'
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    expect(page.value).toBe('history')
    expect(isVoiceSubFlow.value).toBe(false)

    window.location.hash = '#/workshop'
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    expect(page.value).toBe('workshop')
    expect(isVoiceSubFlow.value).toBe(true)

    window.location.hash = '#/nope'
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    expect(page.value).toBe('workbench')
  })
})
