import { describe, expect, it } from 'vitest'
import railSource from './IconRail.vue?raw'

describe('IconRail contract', () => {
  it('renders page navigation without context panels', () => {
    expect(railSource).toContain("key: 'synthesis'")
    expect(railSource).not.toContain("key: 'drafts'")
    expect(railSource).not.toContain("key: 'queue'")
    expect(railSource).not.toContain("key: 'refaudio'")
    expect(railSource).not.toContain('<slot name="drafts" />')
    expect(railSource).not.toContain('<slot name="queue" />')
    expect(railSource).not.toContain('<slot name="refaudio" />')
  })

  it('exposes queue progress on the synthesis destination', () => {
    expect(railSource).toContain('queueBadge')
    expect(railSource).toContain('queueProgress')
    expect(railSource).toContain("item.key === 'synthesis' && queueProgress !== null && queueProgress !== undefined")
    expect(railSource).toContain('合成 · 队列')
  })

  it('uses graphical labels without replacing the compact rail', () => {
    expect(railSource).toContain(':class="{ expanded, dragging: resizing }"')
    expect(railSource).toContain("import {\n  AudioWaveform,\n  BrainCircuit,\n  ChevronLeft,\n  ChevronRight,\n  Clock,\n  SlidersHorizontal,\n} from '@lucide/vue'")
    expect(railSource).toContain('<component :is="item.icon" class="rail-glyph" aria-hidden="true" />')
    expect(railSource).toContain('<span class="rail-label">{{ item.label }}</span>')
    expect(railSource).toContain("{ key: 'synthesis', icon: AudioWaveform, label: '合成' }")
    expect(railSource).toContain("{ key: 'training', icon: BrainCircuit, label: '训练' }")
    expect(railSource).toContain("{ key: 'history', icon: Clock, label: '历史' }")
    expect(railSource).toContain("{ key: 'settings', icon: SlidersHorizontal, label: '设置' }")
    expect(railSource).not.toContain('AudioLines')
    expect(railSource).not.toContain('FileText')
    expect(railSource).not.toContain('ListOrdered')
  })

  it('replaces synthesis text with a progress ring and tooltip while running', () => {
    expect(railSource).toContain('class="rail-progress"')
    expect(railSource).toContain("'--rail-progress'")
    expect(railSource).toContain('`合成 · 队列 ${queueBadge}`')
    expect(railSource).not.toContain("icon: '队'")
  })

  it('toggles and remembers the expanded rail defensively', () => {
    expect(railSource).toContain('readExpandedPreference()')
    expect(railSource).toContain('typeof globalThis.localStorage')
    expect(railSource).toContain("globalThis.localStorage.getItem('xianghua.rail-expanded')")
    expect(railSource).toContain("globalThis.localStorage.setItem('xianghua.rail-expanded', expanded.value ? '1' : '0')")
    expect(railSource).toContain(':aria-expanded="expanded"')
    expect(railSource).toContain('aria-controls="rail-items"')
  })

  it('publishes current page state without context flyouts', () => {
    expect(railSource).toContain(":aria-current=\"page === item.key ? 'page' : undefined\"")
    expect(railSource).toContain('aria-label="主导航"')
    expect(railSource).not.toContain('rail-flyout')
    expect(railSource).not.toContain('icon-rail-context')
    expect(railSource).not.toContain('openPanel')
  })

  it('keeps the rail control at the top of the rail', () => {
    const toggleStart = railSource.indexOf('class="rail-btn rail-toggle"')
    const itemsStart = railSource.indexOf('id="rail-items"')
    expect(toggleStart).toBeGreaterThan(-1)
    expect(itemsStart).toBeGreaterThan(toggleStart)
  })

  it('exposes a horizontal resizer for the expanded rail', () => {
    expect(railSource).toContain("const railWidth = () => (expanded.value ? expandedWidth.value : 44)")
    expect(railSource).toContain("'--rail-w'")
    expect(railSource).toContain('class="rail-resizer"')
    expect(railSource).toContain('role="separator"')
    expect(railSource).toContain('aria-orientation="vertical"')
    expect(railSource).toContain('@pointerdown="onResizeStart"')
    expect(railSource).toContain('@pointermove="onResizeMove"')
    expect(railSource).toContain('@pointerup="onResizeEnd"')
    expect(railSource).toContain('@pointercancel="onResizeEnd"')
    expect(railSource).toContain('const rawWidth = resizeStartWidth + resizeStartX - event.clientX')
    expect(railSource).toContain('RAIL_COLLAPSE_WIDTH')
    expect(railSource).toContain('ArrowLeft')
    expect(railSource).toContain('ArrowRight')
  })

  it('points the right-edge toggle in the action direction', () => {
    expect(railSource).toContain('<component :is="expanded ? ChevronRight : ChevronLeft" class="rail-glyph" aria-hidden="true" />')
  })

  it('uses larger navigation targets and a wider expanded rail', () => {
    expect(railSource).toContain('const DEFAULT_EXPANDED_RAIL_WIDTH = 168')
    expect(railSource).toContain('const MIN_EXPANDED_RAIL_WIDTH = 148')
    expect(railSource).toContain('const MAX_EXPANDED_RAIL_WIDTH = 300')
  })
})
