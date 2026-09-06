import { describe, expect, it } from 'vitest'
import swallowSource from './OrbSwallow.vue?raw'

describe('OrbSwallow contract', () => {
  it('rises sampled characters from the editor into the orb', () => {
    expect(swallowSource).toContain('orb-swallow-char')
    expect(swallowSource).toContain('animationDelay')
    expect(swallowSource).toContain("emit('done')")
  })

  it('keeps sampling bounded so long text does not flood the stage', () => {
    expect(swallowSource).toContain('Math.min(14')
  })

  it('re-triggers cleanly and resets previous characters', () => {
    expect(swallowSource).toContain('window.clearTimeout(doneTimer)')
    expect(swallowSource).toContain('chars.value = []')
  })

  it('clears the pending done timer when unmounted mid-animation', () => {
    expect(swallowSource).toContain('onBeforeUnmount(() => window.clearTimeout(doneTimer))')
  })
})
