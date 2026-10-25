import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import AudioPlayer from './AudioPlayer.vue'

describe('AudioPlayer', () => {
  beforeEach(() => {
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue()
    vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
  })

  it('renders a hidden audio element and a play control', () => {
    const wrapper = mount(AudioPlayer, { props: { src: '/api/files/1' } })
    expect(wrapper.find('audio').attributes('src')).toBe('/api/files/1')
    expect(wrapper.get('.player-toggle').attributes('aria-label')).toBe('播放')
    expect(wrapper.findAll('.player-time')).toHaveLength(2)
  })

  it('toggles playback and emits play/pause', async () => {
    const wrapper = mount(AudioPlayer, { props: { src: '/api/files/1' } })
    await wrapper.get('.player-toggle').trigger('click')
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalled()
    expect(wrapper.get('.player-toggle').attributes('aria-label')).toBe('播放')

    const audio = wrapper.get('audio')
    await audio.trigger('play')
    expect(wrapper.emitted('play')).toHaveLength(1)
    expect(wrapper.get('.player-toggle').attributes('aria-label')).toBe('暂停')

    await audio.trigger('pause')
    expect(wrapper.emitted('pause')).toHaveLength(1)
    expect(wrapper.get('.player-toggle').attributes('aria-label')).toBe('播放')
  })
})
